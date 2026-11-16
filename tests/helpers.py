"""Fixture builders shared by the test modules."""

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image


def img_bytes(fmt, mode="RGB", size=(300, 400), **kw):
    im = (
        Image.effect_noise(size, 40).convert(mode)
        if mode != "P"
        else Image.effect_noise(size, 40).convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE)
    )
    b = io.BytesIO()
    im.save(b, fmt, **kw)
    return b.getvalue()


def png_transparent():
    im = Image.effect_noise((300, 400), 40).convert("RGBA")
    im.putalpha(128)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def gif_animated():
    frames = [Image.new("RGB", (50, 50), c) for c in ("red", "green", "blue")]
    b = io.BytesIO()
    frames[0].save(b, "GIF", save_all=True, append_images=frames[1:])
    return b.getvalue()


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


def comic_entries():
    """Every case the converter must handle, in one archive."""
    return {
        "001.jpg": img_bytes("JPEG", quality=95),
        "002.png": png_transparent(),
        "003.png": img_bytes("PNG", "P"),
        "001.png": img_bytes("PNG", "L"),  # collides with 001.jpg -> 001.webp
        "004.webp": img_bytes("WEBP"),
        "005.gif": gif_animated(),
        "ComicInfo.xml": b"<ComicInfo><Title>x</Title></ComicInfo>",
        "__MACOSX/._001.jpg": b"junk",
        ".DS_Store": b"junk",
    }
