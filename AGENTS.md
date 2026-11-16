# AGENTS.md

Instructions for coding agents working in this repository. Read `README.md` for what the tool does for its users.

## What this repo is

`cbz-webp` recompresses the images inside `.zip` and `.cbz` archives to WebP and writes `.cbz` files. Work goes through an in-memory job queue that a CLI (`convert`) or a local web console (`ui`) drives.

It is one of three related repos:

| Repo | What it is |
|---|---|
| `cbz-webp` (this one, public) | The converter |
| `dl-queue` (public) | A download queue with API-key support. Independent of this repo |
| `harbor` (private) | Installs both and serves them on one two-tab page. It imports `cbzwebp.web.serve`, `ConvertAPI` and `JobQueue`, so treat those as public API |

| Path | What it is |
|---|---|
| `cbzwebp/core.py` | Scanning folders and converting one archive. All file writes, renames and deletes happen here |
| `cbzwebp/jobqueue.py` | `BaseQueue` (pending list, workers, pause, cancel, retry, clear) and `JobQueue` (archive workers plus one shared image-encoding pool) |
| `cbzwebp/web.py` | The generic console server (`serve`, `list_dirs`, request guards), `ConvertAPI`, and `serve_convert` for this tool's page |
| `cbzwebp/ui.html` | The console page: one file, CSS and JS inline, no external requests |
| `cbzwebp/cli.py` | `convert` and `ui` subcommands |
| `bin/cbz-webp` | Runs the CLI from a checkout (follows symlinks) |
| `tests/` | `unittest` suites; `helpers.py` builds the fixture archives |
| `ci/release_notes.py` | Release gate: tag must equal `v<__version__>`; prints that version's CHANGELOG section |
| `.github/workflows/` | `ci.yml` (lint, tests, package smoke test) and `release.yml` (tag → GitHub release) |

## The tool deletes user files

By default `convert` deletes each original `.zip`, so the conversion must never lose data. These rules hold today, and the tests cover rules 3 to 7 directly. Keep them all:

1. Output goes to `<Name>.cbz.part` in the **same folder** as the source, so the final `os.replace` stays on one file system.
2. The `.part` is reopened and checked before it is renamed: `testzip()`, the entry list equals what was written, every WebP decodes (`_verify`).
3. The source `.zip` is deleted only after the rename succeeded. Never on skip, failure or cancel. A failed delete is reported, not raised.
4. Any exception or cancel removes the `.part` and leaves the source as it was.
5. Archives with no images, `.cbz` files that are already all WebP, and a `.zip` whose `.cbz` already exists (without `overwrite`) are skipped without being rewritten.
6. Two jobs with the same output file never run at the same time (`JobQueue._busy_key`). `Name.zip` and `Name.cbz` both write `Name.cbz`.
7. An image that can't be converted safely (animated, larger than 16,383 px) is copied unchanged, never dropped. An image that can't be decoded fails the whole archive.

If a change touches any of these, add or update a test that fails without your change.

## Never touch the user's real files while testing

- Run the tool only against throwaway folders in a temp dir, as the tests do. Never against the user's comic library or any folder you did not create.
- To look at a real folder, use `convert <folder> --dry-run`. It only lists files.
- To try the console, start your own copy on a free port (`bin/cbz-webp ui -p <port>`) and stop it by PID when done. Do not stop a console you did not start. Avoid `pkill -f` with a pattern that also appears in your own shell command, because it kills that shell.

## Test and lint

```bash
python3 -m unittest discover -s tests
ruff check . && ruff format --check .
```

Both must pass before you hand work back. CI pins ruff (`RUFF_VERSION` in `ci.yml`), so use that version. On Ubuntu without a venv, install it with `pip install --target <dir> ruff==<version>`. Add tests for any new behaviour: `tests/test_cbzwebp.py` for core and queue, `tests/test_cli_web.py` for CLI flags, HTTP endpoints and the composed server, `tests/test_release_notes.py` for the release gate. Tests build their own temp folders and clean up; keep it that way.

## Rules for the code

- **One dependency: Pillow** (with WebP support). Everything else is the standard library, and the page is plain JS with no CDN.
- **Python 3.10+.** CI tests 3.10 through 3.14. Don't use anything newer than 3.10 without raising `requires-python` in `pyproject.toml` and the CI matrix together.
- **Changes to files go through `core.convert_archive`.** The queue, CLI and web layers never write, rename or delete archives themselves. `list_dirs` only reads.
- **The console stays local and guarded.** It binds `127.0.0.1` only. `POST` requires `Content-Type: application/json`, a `Host` of `127.0.0.1:<port>` or `localhost:<port>`, and a same-origin `Origin` if one is sent. These stop another website from driving a page that deletes files. `GET` checks `Host` too, which stops DNS rebinding from reading `/api/state` or `/api/dirs`. New endpoints are actions on an API object (`ConvertAPI.post`), never new branches in the handler, so every one gets the same checks. An API raises `ValueError` for bad input, and the server turns it into a 400.
- **Queue state changes happen under `self._cond`.** Call `_emit` outside the lock. Workers check `job.cancel` between images only, so keep per-image work short.
- **The page must work offline and in dark mode.** Colours are CSS variables on `:root`, with a `prefers-color-scheme: dark` override. `render()` rewrites only the stats and table body every second; keep the form and the folder-picker `<dialog>` outside those, or the poll will reset what the user is typing.
- **Keep the shared code in step with dl-queue.** dl-queue can't depend on this repo (it would pull in Pillow), so it carries copies. harbor's tests compare the three Python copies and fail when they drift; the page markup you compare by eye. When you change one side, change the other:

  | Here | In dl-queue |
  |---|---|
  | `class BaseQueue` in `cbzwebp/jobqueue.py` | `class BaseQueue` in `dlqueue/basequeue.py` |
  | the generic server part of `cbzwebp/web.py` (between the `generic server part` markers) | the same part of `dlqueue/web.py` |
  | `normalize_path` in `cbzwebp/core.py` | `normalize_path` in `dlqueue/paths.py` |
  | the folder-picker markup, CSS and JS in `ui.html` | the same in `dlqueue/ui.html` and harbor's `harbor/ui.html` |

## Releasing

1. Set `__version__` in `cbzwebp/__init__.py` (the only place the version lives; `pyproject.toml` reads it).
2. Move the `## Unreleased` notes in `CHANGELOG.md` under a new `## <version>` heading.
3. Commit, push to `main`, wait for CI to pass.
4. `git tag v<version> && git push origin v<version>`.

`release.yml` then checks the tag against `__version__` and the CHANGELOG, runs the full CI, builds the sdist and wheel, and publishes a GitHub release with them, a `SHA256SUMS` file and the CHANGELOG section as notes. A tag with a `-` (`v0.2.0-rc1`) becomes a pre-release. harbor pins this repo by tag, so a release is how a change reaches it. Tagging and pushing tags publishes to GitHub, so do it only when the user asks for a release.
