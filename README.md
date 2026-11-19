# cbz-webp

[![CI](https://github.com/syahmizainudin/cbz-webp/actions/workflows/ci.yml/badge.svg)](https://github.com/syahmizainudin/cbz-webp/actions/workflows/ci.yml)

`cbz-webp` goes through a folder, recompresses the images inside every `.zip` and `.cbz` archive to WebP at the quality you choose, and saves each result as a `.cbz`. Archives go through a queue, which you drive from the command line or from a local web page.

## Install

Needs Python 3.10 or later. Pick one:

```bash
# From GitHub with pipx (installs Pillow for you)
pipx install git+https://github.com/syahmizainudin/cbz-webp

# Or from a checkout; needs Pillow with WebP support (sudo apt install python3-pil, or pip)
git clone https://github.com/syahmizainudin/cbz-webp.git
ln -s "$PWD/cbz-webp/bin/cbz-webp" ~/.local/bin/cbz-webp
```

Each [release](https://github.com/syahmizainudin/cbz-webp/releases) also has a wheel you can install with `pipx install cbz_webp-<version>-py3-none-any.whl`.

## Commands

| Command | What it does |
|---|---|
| `cbz-webp convert <folder>... [-q 80]` | Scans the folders (subfolders included), queues every archive, converts them and exits |
| `cbz-webp convert <folder> --dry-run` | Lists the archives it would queue and changes nothing |
| `cbz-webp ui [-p 8765] [--open]` | Starts the web console at `http://127.0.0.1:8765/` |

Options:

| Option | Default | Meaning |
|---|---|---|
| `-q`, `--quality` | 80 | WebP quality, 1 to 100 |
| `-m`, `--method` | 4 | Encoder effort, 0 to 6. Lower is faster; higher gives smaller files at the same quality. 2 or 3 is noticeably faster on large libraries |
| `-j`, `--jobs` | 1 | Archives converted at the same time |
| `-t`, `--threads` | CPU count | Image-encoding threads, shared by all jobs |
| `--keep` | off | Keep the original `.zip` |
| `--overwrite` | off | Replace a `Name.cbz` that already sits next to `Name.zip` |
| `--no-recursive` | off | Scan only the given folders, not their subfolders |

`convert` exits with status 1 if any archive failed, and 130 if you cancelled it. Ctrl+C cancels the queue and leaves every original untouched.

You can also type Windows paths such as `C:\Comics` in WSL. The tool reads them as `/mnt/c/Comics`.

## Web console

In the console you can:

- Add folders or files, one per line, or click **Open folder…** to browse to one. The browser shows WSL's folders, with shortcuts to your home folder and each Windows drive (`/mnt/c`, `/mnt/d`, ...).
- Set the quality, the encoder effort and whether the original `.zip` is deleted. These apply to the jobs you add next.
- Watch per-archive progress, the size before and after, and the total saved.
- Cancel a queued or running job, or retry a failed or cancelled one. Pause and resume the queue, and clear finished jobs.

The queue lives in memory, so stopping the console forgets it. To pick up where you stopped, add the same folder again: converted `.zip` files are gone, and `.cbz` files that are already all WebP are skipped. The console listens only on `127.0.0.1` and accepts only same-origin JSON requests, so another website cannot send it commands.

To download files from URLs, use [dl-queue](https://github.com/syahmizainudin/dl-queue).

### Watch folders

`cbz-webp ui --watch /mnt/d/Inbox` converts every `.zip` or `.cbz` that appears in that folder (or its subfolders) without you clicking anything. Give `--watch` more than once for several folders. Every 30 seconds the folders are checked, and a file is queued once it has stopped changing, so a file still being copied waits. Each file is converted once; one that fails is tried again only after it changes. Originals are **kept** unless you add `--watch-delete`. After a restart, files still in the folders are checked once more.

### Using the console in another tool

`cbzwebp.web.serve(port, page, apis)` serves any page with any set of APIs, each mounted at a URL prefix. `ConvertAPI` is this tool's convert queue as one of those APIs. Each API has `state() -> dict`, which is merged into `GET /api/state`, and `post(action, body) -> (status, dict)`, which answers `POST <prefix><action>`. The server adds the request checks and the folder picker's `GET /api/dirs`. `allowed_origins` lists exact extra origins whose requests are accepted, such as a browser extension's `chrome-extension://<id>`.

By default the console listens on 127.0.0.1 only. A tool that puts it on a network passes `serve(..., host=, allowed_hosts=, ssl_context=, trust_proxy=)` and is responsible for doing that safely; harbor does, with accounts and HTTPS required.

## What happens to each archive

| Input | Result |
|---|---|
| `Name.zip` | Writes `Name.cbz` next to it. Deletes `Name.zip` after the new file passes the checks, unless you pass `--keep` |
| `Name.zip` when `Name.cbz` already exists | Skipped, both files left alone (unless `--overwrite`) |
| `Name.cbz` with JPG/PNG/other images | Recompressed and replaced in place |
| `Name.cbz` that is already all WebP | Skipped, not rewritten |
| Archive with no images | Skipped, not deleted |
| Archive with an unreadable image, or not a valid zip | Fails, original left alone |

Inside each archive:

- JPG, PNG, BMP, GIF and TIFF images become WebP. Existing WebP images are copied unchanged.
- The tool copies animated GIFs, and images wider or taller than 16,383 px (the WebP limit), unchanged, and lists them as "kept as-is".
- It keeps transparency, and applies EXIF rotation so pages display the right way up.
- Other files, such as `ComicInfo.xml`, are copied byte for byte. `__MACOSX/`, `.DS_Store` and `Thumbs.db` are dropped.
- It keeps file order and names, with the extension changed to `.webp`. If two pages would end up with the same name, as `01.jpg` and `01.png` would, the second becomes `01.png.webp`.

### Before an original is deleted

1. The new archive is written to `Name.cbz.part` in the same folder.
2. The `.part` is reopened: every CRC is checked, the list of files must match, and every WebP image must decode.
3. Only then is it renamed to `Name.cbz`, and only after that is `Name.zip` deleted.

If any step fails, or you cancel, the tool removes the `.part` and does not touch the original.

## CPU or GPU?

Encoding runs on the CPU, through Pillow's libwebp, on all cores by default. libwebp has no GPU encoder, and GPU video encoders don't produce WebP images. To go faster, lower `--method`, and if many archives are small, raise `--jobs`.

## Development

```bash
python3 -m unittest discover -s tests       # tests
ruff check . && ruff format --check .        # lint, same as CI
```

GitHub Actions runs lint, the tests on Python 3.10 to 3.14, and a package smoke test on every push to `main` and every pull request. Pushing a `v<version>` tag publishes a GitHub release; the steps are in [AGENTS.md](AGENTS.md#releasing), which also holds the rules for changing the code. Changes are listed in [CHANGELOG.md](CHANGELOG.md).
