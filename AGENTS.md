# AGENTS.md

Instructions for coding agents working in this repository. Read `README.md` for what the tool does for its users.

## What this repo is

`cbz-webp` recompresses the images inside `.zip` and `.cbz` archives to WebP and writes `.cbz` files. Work goes through an in-memory job queue that a CLI (`convert`) or a local web console (`ui`) drives. The console also has a download queue that fetches files from direct URLs, with an API key, and can pass archives on to the convert queue.

| Path | What it is |
|---|---|
| `cbzwebp/core.py` | Scanning folders and converting one archive. All file writes, renames and deletes happen here |
| `cbzwebp/jobqueue.py` | `BaseQueue` (pending list, workers, pause, cancel, retry, clear) and `JobQueue` (convert: archive workers plus one shared image-encoding pool) |
| `cbzwebp/download.py` | `DownloadQueue`: fetches URLs with `urllib`, API key handling, file naming, hand-off to `JobQueue` |
| `cbzwebp/web.py` | The console server: `http.server` from the standard library, JSON API, request guards, `list_dirs` for the folder picker |
| `cbzwebp/ui.html` | The console page: one file, CSS and JS inline, no external requests |
| `cbzwebp/cli.py` | `convert` and `ui` subcommands |
| `bin/cbz-webp` | Runs the CLI from a checkout (follows symlinks) |
| `tests/` | `unittest` suites. `helpers.py` builds the fixture archives and `FileServer`, a local HTTP server for download tests |
| `ci/release_notes.py` | Release gate: tag must equal `v<__version__>`; prints that version's CHANGELOG section |
| `.github/workflows/` | `ci.yml` (lint, tests, package smoke test) and `release.yml` (tag → GitHub release) |

## The tool deletes user files

By default `convert` deletes each original `.zip`, so the conversion must never lose data. These rules hold today, and the tests cover rules 3 to 7 directly. Keep them all:

1. Output goes to `<Name>.cbz.part` in the **same folder** as the source, so the final `os.replace` stays on one file system.
2. The `.part` is reopened and checked before it is renamed: `testzip()`, the entry list equals what was written, every WebP decodes (`_verify`).
3. The source `.zip` is deleted only after the rename succeeded. Never on skip, failure or cancel. A failed delete is reported, not raised.
4. Any exception or cancel removes the `.part` and leaves the source as it was.
5. Archives with no images, `.cbz` files that are already all WebP, and a `.zip` whose `.cbz` already exists (without `overwrite`) are skipped without being rewritten.
6. Two jobs with the same output file never run at the same time (`JobQueue._next_runnable`). `Name.zip` and `Name.cbz` both write `Name.cbz`.
7. An image that can't be converted safely (animated, larger than 16,383 px) is copied unchanged, never dropped. An image that can't be decoded fails the whole archive.

Downloads write files too, so the same care applies to `download.py`:

8. Data goes to `<name>.part` in the destination folder and is renamed only when complete (byte count equals `Content-Length` when one was sent). Cancel, failure or a short read removes the `.part`.
9. A download never overwrites a file: `_reserve` picks `name (1).ext` and holds the name under the lock, so two concurrent downloads can't take the same name. Names from `Content-Disposition` or the URL go through `safe_name`, which strips directories (no `../`) and characters Windows forbids.
10. The API key is secret. It never appears in `to_dict()`, `repr()`, messages or logs. In query mode the stored URL has no key, which is added only when the request is made. Auth headers use `add_unredirected_header`, so a redirect to another host never receives the key. Header and parameter names must be RFC 9110 tokens, and keys may not contain line breaks.

If a change touches any of these, add or update a test that fails without your change.

## Never touch the user's real files while testing

- Run the tool only against throwaway folders in a temp dir, as the tests do. Never against the user's comic library or any folder you did not create.
- Test downloads only against `helpers.FileServer` on `127.0.0.1`. Never download from a real URL in tests, and never put a real API key in a test, a fixture or a commit.
- To look at a real folder, use `convert <folder> --dry-run`. It only lists files.
- To try the console, start your own copy on a free port (`bin/cbz-webp ui -p <port>`) and stop it by PID when done. Do not stop a console you did not start. Avoid `pkill -f` with a pattern that also appears in your own shell command, because it kills that shell.

## Test and lint

```bash
python3 -m unittest discover -s tests
ruff check . && ruff format --check .
```

Both must pass before you hand work back. CI pins ruff (`RUFF_VERSION` in `ci.yml`), so use that version. On Ubuntu without a venv, install it with `pip install --target <dir> ruff==<version>`. Add tests for any new behaviour: `tests/test_cbzwebp.py` for core and queue, `tests/test_download.py` for the download queue, `tests/test_cli_web.py` for CLI flags and HTTP endpoints, `tests/test_release_notes.py` for the release gate. Tests build their own temp folders and clean up; keep it that way.

## Rules for the code

- **One dependency: Pillow** (with WebP support). Everything else is the standard library (downloads use `urllib`, not `requests`), and the page is plain JS with no CDN.
- **Python 3.10+.** CI tests 3.10 through 3.14. Don't use anything newer than 3.10 without raising `requires-python` in `pyproject.toml` and the CI matrix together.
- **Two modules write files, and nothing else does.** `core.convert_archive` writes, replaces and deletes archives; `DownloadQueue._run` writes downloads. The queue base, CLI and web layers never touch files themselves. `list_dirs` only reads.
- **New queues extend `BaseQueue`.** Implement `_run(job)`; override `_dedup_key` (what counts as a duplicate) and `_busy_key` (what may not run at the same time) as needed. Jobs need `fresh()` for retry and a `to_dict()` that contains nothing secret.
- **The console stays local and guarded.** It binds `127.0.0.1` only. `POST` requires `Content-Type: application/json`, a `Host` of `127.0.0.1:<port>` or `localhost:<port>`, and a same-origin `Origin` if one is sent. These stop another website from driving a page that deletes files. `GET` checks `Host` too, which stops DNS rebinding from reading `/api/state` or `/api/dirs`. Any new endpoint goes in `do_POST` (or under `/api/download/` in `_download`) behind the same checks, gets a test in `WebTests`, and catches `OSError` and `ValueError` so it returns a 400 instead of dropping the connection.
- **Queue state changes happen under `self._cond`.** Call `_emit` outside the lock. Workers check `job.cancel` between images only, so keep per-image work short.
- **The page must work offline and in dark mode.** Colours are CSS variables on `:root`, with a `prefers-color-scheme: dark` override. `render()` rewrites only the stats and table bodies every second; keep forms and the folder-picker `<dialog>` outside those, or the poll will reset what the user is typing. Only per-browser conveniences go in `localStorage` (tab, save-to folder, auth mode), and the API key only when the user ticks "Remember".

## Releasing

1. Set `__version__` in `cbzwebp/__init__.py` (the only place the version lives; `pyproject.toml` reads it).
2. Move the `## Unreleased` notes in `CHANGELOG.md` under a new `## <version>` heading.
3. Commit, push to `main`, wait for CI to pass.
4. `git tag v<version> && git push origin v<version>`.

`release.yml` then checks the tag against `__version__` and the CHANGELOG, runs the full CI, builds the sdist and wheel, and publishes a GitHub release with them, a `SHA256SUMS` file and the CHANGELOG section as notes. A tag with a `-` (`v0.2.0-rc1`) becomes a pre-release. Tagging and pushing tags publishes to GitHub, so do it only when the user asks for a release.
