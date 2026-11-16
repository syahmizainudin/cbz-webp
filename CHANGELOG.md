# Changelog

Each release has a `## <version>` section. The release pipeline publishes that section as the GitHub release notes, and refuses to release a version without one.

## Unreleased

- The folder picker keeps one size; long folder lists scroll inside it instead of resizing the window.

## 0.3.0

- The console server can serve more than JSON and one page, for tools built on it (cbz-shelf's library and reader):
  - an API may define `get(action, query)` to answer `GET <prefix><action>` with any content type and its own `Cache-Control`;
  - `serve(..., pages={"/library": html})` serves extra pages.
- Pages may now be framed by pages from the same origin (`X-Frame-Options: SAMEORIGIN`, was `DENY`), so harbor can show another tool's page in a tab. Other sites still can't frame it.
- `core.natural_key` is public.

## 0.2.0

- Encoder effort setting: `-m`/`--method` 0–6 on `convert` and `ui`, a dropdown on the page, and `method` in `JobQueue.add` and the `add` endpoint. Default 4, as before.
- `serve(..., allowed_origins=...)` accepts requests from exact extra origins, such as a browser extension. The page's own origin must now be plain `http`.

## 0.1.0

First release.

- `cbz-webp convert` scans folders (subfolders included), queues every `.zip` and `.cbz`, recompresses the images to WebP at the chosen quality and writes `.cbz` files.
- `cbz-webp ui` serves a local web console to add folders, watch progress, cancel, retry, pause and resume. **Open folder…** browses WSL folders, with shortcuts to home and each Windows drive.
- The console's server is reusable: `serve(port, page, apis)` mounts any number of APIs by URL prefix, and `ConvertAPI` is the convert queue as one of them.
- The original `.zip` is deleted only after the new `.cbz` is written, reopened and checked; `--keep` keeps it.
- Archives with no images, `.cbz` files that are already all WebP, and a `.zip` with a `.cbz` already beside it are skipped and left untouched.
- Two jobs that write the same `.cbz` (`Name.zip` and `Name.cbz` with `--overwrite`) never run at the same time.
