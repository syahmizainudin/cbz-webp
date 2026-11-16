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
| `-j`, `--jobs` | 1 | Archives converted at the same time |
| `-t`, `--threads` | CPU count | Image-encoding threads, shared by all jobs |
| `--keep` | off | Keep the original `.zip` |
| `--overwrite` | off | Replace a `Name.cbz` that already sits next to `Name.zip` |
| `--no-recursive` | off | Scan only the given folders, not their subfolders |

`convert` exits with status 1 if any archive failed, and 130 if you cancelled it. Ctrl+C cancels the queue and leaves every original untouched.

You can also type Windows paths such as `C:\Comics` in WSL. The tool reads them as `/mnt/c/Comics`.

## Web console

In the console you can:

- add folders or files to the queue, with their own quality and delete setting
- watch per-archive progress, the size before and after, and the total saved
- cancel a queued or running job, retry a failed or cancelled one
- pause and resume the queue, and clear finished jobs

The queue lives in memory, so stopping the console forgets it. Run `convert` again on the same folder to pick up where you stopped: converted `.zip` files are gone and all-WebP `.cbz` files are skipped. The console listens only on `127.0.0.1` and accepts only same-origin JSON requests, so another website cannot send it commands.

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

## Development

```bash
python3 -m unittest discover -s tests       # tests
ruff check . && ruff format --check .        # lint, same as CI
```

GitHub Actions runs lint, the tests on Python 3.10 to 3.14, and a package smoke test on every push to `main` and every pull request. Pushing a `v<version>` tag publishes a GitHub release; the steps are in [AGENTS.md](AGENTS.md#releasing), which also holds the rules for changing the code. Changes are listed in [CHANGELOG.md](CHANGELOG.md).
