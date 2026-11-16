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
| `cbz-webp ui [-p 8765] [-d 2] [--open]` | Starts the web console at `http://127.0.0.1:8765/`, with a Convert tab and a Download tab |

Options:

| Option | Default | Meaning |
|---|---|---|
| `-q`, `--quality` | 80 | WebP quality, 1 to 100 |
| `-j`, `--jobs` | 1 | Archives converted at the same time |
| `-t`, `--threads` | CPU count | Image-encoding threads, shared by all jobs |
| `--keep` | off | Keep the original `.zip` |
| `--overwrite` | off | Replace a `Name.cbz` that already sits next to `Name.zip` |
| `--no-recursive` | off | Scan only the given folders, not their subfolders |
| `-d`, `--downloads` | 2 | `ui` only: downloads running at the same time |

`convert` exits with status 1 if any archive failed, and 130 if you cancelled it. Ctrl+C cancels the queue and leaves every original untouched.

You can also type Windows paths such as `C:\Comics` in WSL. The tool reads them as `/mnt/c/Comics`.

## Web console

The console has two tabs. Each has its own queue, which lives in memory, so stopping the console forgets both. The console listens only on `127.0.0.1` and accepts only same-origin JSON requests, so another website cannot send it commands.

### Convert tab

- Add folders or files, one per line, or click **Open folder…** to browse to one. The browser shows WSL's folders, with shortcuts to your home folder and each Windows drive (`/mnt/c`, `/mnt/d`, ...).
- Set the quality and whether the original `.zip` is deleted. These apply to the jobs you add next.
- Watch per-archive progress, the size before and after, and the total saved.
- Cancel a queued or running job, or retry a failed or cancelled one. Pause and resume the queue, and clear finished jobs.

To pick up where you stopped after a restart, add the same folder again. Converted `.zip` files are gone, and `.cbz` files that are already all WebP are skipped.

### Download tab

Downloads files from direct URLs into a folder you choose.

- **File URLs:** one per line, each pointing straight at a file. `http` and `https` only.
- **Save to folder:** must already exist; **Open folder…** browses to it. An existing file is never overwritten: a second `Chapter 01.zip` is saved as `Chapter 01 (1).zip`. The file name comes from the server's `Content-Disposition` header, or else from the URL.
- **API key sent as:** `Authorization: Bearer <key>`, a header you name (such as `X-API-Key`), a query parameter you name (such as `?api_key=`), or no key.
- **Convert .zip / .cbz downloads:** on by default. Each downloaded archive goes into the Convert queue, using the Convert tab's quality and delete settings. Other files are only saved.

About the API key:

- The key stays in the console's memory for the jobs that use it. It is never shown in the queue, written to disk or printed.
- If the server redirects a download to another address (a CDN, for example), the key is not sent there.
- The page remembers the key in your browser only if you tick **Remember the key in this browser**.

Each file downloads to `<name>.part` and is renamed when complete. A cancelled, failed or incomplete download leaves nothing behind.

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
