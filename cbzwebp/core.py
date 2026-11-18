"""Archive scanning and the zip/cbz -> WebP cbz conversion itself.

Safety rules (the original may be deleted, so these matter):
  * output is written to ``<name>.cbz.part`` in the source's own folder;
  * the .part is reopened and checked (CRC, entry list, every WebP decodes) before it
    is renamed into place;
  * the source .zip is removed only after that rename succeeded, and never on skip,
    failure or cancel.
"""

from __future__ import annotations

import io
import os
import re
import threading
import zipfile
from collections import deque
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from collections.abc import Callable, Iterable

from PIL import Image, ImageOps, UnidentifiedImageError

ARCHIVE_EXTS = {".zip", ".cbz"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
CONVERT_EXTS = IMAGE_EXTS - {".webp"}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
WEBP_MAX_SIDE = 16383
DEFAULT_METHOD = 4  # libwebp effort: 0 = fastest, 6 = smallest files
PART_SUFFIX = ".part"

ProgressFn = Callable[[int, int], None]


class Cancelled(Exception):
    """The job was cancelled between images."""


class ConvertError(Exception):
    """The archive could not be converted; the source is left untouched."""


@dataclass
class Result:
    status: str  # "done" | "skipped"
    message: str
    output: str | None = None
    bytes_in: int = 0
    bytes_out: int = 0
    images: int = 0
    converted: int = 0
    kept: list[str] = field(default_factory=list)  # images copied as-is, with reason
    deleted: str | None = None


# ---------------------------------------------------------------- scanning


def normalize_path(raw: str) -> Path:
    """Accept POSIX paths and, for WSL users, Windows paths like ``C:\\Comics``."""
    raw = raw.strip().strip('"').strip("'")
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if m and not Path(raw).exists():
        raw = f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")
    return Path(os.path.expanduser(raw)).resolve()


def natural_key(p) -> list:
    """Sort key that orders 2 before 10 (``Ch 2`` < ``Ch 10``), case-insensitive."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(p))]


def scan(paths: Iterable[str | Path], recursive: bool = True) -> list[Path]:
    """Return every .zip/.cbz under ``paths`` (files are taken as given), naturally sorted."""
    found: dict[Path, None] = {}
    for raw in paths:
        p = normalize_path(str(raw))
        if p.is_file():
            if p.suffix.lower() in ARCHIVE_EXTS:
                found[p] = None
            continue
        if not p.is_dir():
            raise FileNotFoundError(f"not found: {p}")
        it = p.rglob("*") if recursive else p.iterdir()
        for f in it:
            # Extension first: it needs no system call, so only archives get stat'ed.
            if f.suffix.lower() in ARCHIVE_EXTS and f.is_file():
                found[f.resolve()] = None  # resolve: a symlink and its target count once
    return sorted(found, key=natural_key)


# ---------------------------------------------------------------- images


def _ext(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def is_junk(name: str) -> bool:
    parts = PurePosixPath(name).parts
    if any(p == "__MACOSX" for p in parts):
        return True
    base = parts[-1] if parts else name
    return base.lower() in JUNK_NAMES or base.startswith("._")


def encode_webp(data: bytes, quality: int, method: int = 4) -> tuple[bytes | None, str]:
    """Encode one image to WebP.

    Returns ``(webp_bytes, "")`` or ``(None, reason)`` when the image should be copied
    unchanged (animated, too large for WebP). Raises on undecodable data.
    """
    with Image.open(io.BytesIO(data)) as im:
        if getattr(im, "n_frames", 1) > 1:
            return None, "animated"
        im.load()
        ImageOps.exif_transpose(im, in_place=True)  # returns None; no copy when upright
        if max(im.size) > WEBP_MAX_SIDE:
            return None, f"{im.size[0]}x{im.size[1]} exceeds WebP limit"
        if im.mode in ("I", "I;16", "I;16B", "I;16L", "I;16N"):
            im = im.point(lambda v: v / 256).convert("L")
        has_alpha = im.mode in ("RGBA", "LA", "PA", "RGBa", "La") or (
            im.mode == "P" and "transparency" in im.info
        )
        target = "RGBA" if has_alpha else "RGB"
        if im.mode != target:  # LA, PA, P, L, CMYK, ... are converted; RGB/RGBA as they are
            im = im.convert(target)
        out = io.BytesIO()
        im.save(out, "WEBP", quality=quality, method=method)
        return out.getvalue(), ""


# ---------------------------------------------------------------- archives


@dataclass
class _Entry:
    info: zipfile.ZipInfo
    out_name: str
    convert: bool


def _plan(infos: list[zipfile.ZipInfo]) -> list[_Entry]:
    """Decide output names; ``a.jpg`` + ``a.png`` must not both become ``a.webp``."""
    taken = {i.filename for i in infos if _ext(i.filename) not in CONVERT_EXTS}
    plan = []
    for i in infos:
        if _ext(i.filename) in CONVERT_EXTS:
            name = str(PurePosixPath(i.filename).with_suffix(".webp"))
            if name in taken:
                name = i.filename + ".webp"
            taken.add(name)
            plan.append(_Entry(i, name, True))
        else:
            plan.append(_Entry(i, i.filename, False))
    return plan


def _decode(data: bytes) -> None:
    with Image.open(io.BytesIO(data)) as im:
        im.load()


def _verify(part: Path, expected: list[str], webp_names: set[str], *, pool: Executor, window: int) -> None:
    """Reopen the .part on disk and check it in one pass over its entries.

    ``z.read`` checks every entry's CRC, and each WebP is decoded on ``pool``, at most
    ``window`` at a time. Any problem raises ``ConvertError``. Cancel is not checked here.
    """
    decoding: deque[tuple[str, Future]] = deque()

    def check_one() -> None:
        n, fut = decoding.popleft()
        try:
            fut.result()
        except Exception as e:
            raise ConvertError(f"verify: {n} does not decode ({e})") from e

    try:
        with zipfile.ZipFile(part) as z:
            names = z.namelist()
            if names != expected:
                raise ConvertError(f"verify: wrote {len(names)} entries, expected {len(expected)}")
            for n in names:
                try:
                    data = z.read(n)
                except Exception as e:
                    raise ConvertError(f"verify: cannot read {n} ({e})") from e
                if n in webp_names:
                    decoding.append((n, pool.submit(_decode, data)))
                    while len(decoding) > window:
                        check_one()
        while decoding:
            check_one()
    finally:
        for _, fut in decoding:
            fut.cancel()


def convert_archive(
    src: Path,
    quality: int,
    *,
    pool: Executor,
    delete_original: bool = True,
    overwrite: bool = False,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    method: int = 4,
    window: int = 8,
) -> Result:
    """Convert one .zip/.cbz to a WebP .cbz next to it. See module docstring for safety rules.

    ``window`` is how many images may be in flight on ``pool`` at once (encoding, then
    verifying); about twice the pool's threads keeps it busy without holding many pages.
    """
    src = Path(src)
    is_cbz = src.suffix.lower() == ".cbz"
    dest = src if is_cbz else src.with_suffix(".cbz")
    bytes_in = src.stat().st_size

    if not is_cbz and dest.exists() and not overwrite:
        return Result("skipped", f"{dest.name} already exists (use overwrite)", bytes_in=bytes_in)

    try:
        zin = zipfile.ZipFile(src)
    except zipfile.BadZipFile as e:
        raise ConvertError(f"not a valid zip: {e}") from e

    part = dest.with_name(dest.name + PART_SUFFIX)
    window = max(2, window)
    res = Result("done", "", bytes_in=bytes_in)
    pending: deque[tuple[_Entry, bytes, Future | None]] = deque()
    written: list[str] = []
    try:
        with zin:
            infos = [i for i in zin.infolist() if not i.is_dir() and not is_junk(i.filename)]
            images = [i for i in infos if _ext(i.filename) in IMAGE_EXTS]
            if not images:
                return Result("skipped", "no images in archive", bytes_in=bytes_in)
            plan = _plan(infos)
            res.images = len(images)
            if is_cbz and not any(e.convert for e in plan):
                return Result("skipped", "already all WebP", bytes_in=bytes_in, images=len(images))

            total, done = len(plan), 0
            webp_names: set[str] = set()

            def flush_one(zout: zipfile.ZipFile) -> None:
                nonlocal done
                entry, raw, fut = pending.popleft()
                data, name = raw, entry.info.filename
                if fut is not None:
                    try:
                        webp, reason = fut.result()
                    except UnidentifiedImageError as e:
                        raise ConvertError(f"{entry.info.filename}: not a readable image") from e
                    except Exception as e:
                        raise ConvertError(f"{entry.info.filename}: cannot decode ({e})") from e
                    if webp is None:
                        res.kept.append(f"{entry.info.filename}: {reason}")
                    else:
                        data, name = webp, entry.out_name
                        webp_names.add(name)
                        res.converted += 1
                zi = zipfile.ZipInfo(name, date_time=entry.info.date_time)
                is_img = _ext(name) in IMAGE_EXTS
                zi.compress_type = zipfile.ZIP_STORED if is_img else zipfile.ZIP_DEFLATED
                zout.writestr(zi, data)
                written.append(name)
                done += 1
                if progress:
                    progress(done, total)

            if progress:
                progress(0, total)
            with zipfile.ZipFile(part, "w") as zout:
                for entry in plan:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    raw = zin.read(entry.info)
                    fut = pool.submit(encode_webp, raw, quality, method) if entry.convert else None
                    pending.append((entry, raw, fut))
                    while len(pending) > window:
                        flush_one(zout)
                while pending:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    flush_one(zout)

        _verify(part, written, webp_names, pool=pool, window=window)
        os.replace(part, dest)
    except BaseException:
        for _, _, fut in pending:
            if fut is not None:
                fut.cancel()
        part.unlink(missing_ok=True)
        raise

    res.output = str(dest)
    res.bytes_out = dest.stat().st_size
    if delete_original and not is_cbz:
        try:
            src.unlink()
            res.deleted = str(src)
        except OSError as e:
            res.kept.append(f"original not deleted: {e}")
    change = 100 * (res.bytes_out / bytes_in - 1) if bytes_in else 0
    res.message = f"{res.converted}/{res.images} images to WebP, size {change:+.0f}%"
    if res.kept:
        res.message += f"; {len(res.kept)} kept as-is"
    return res
