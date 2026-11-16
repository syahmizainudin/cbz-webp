# Changelog

Each release has a `## <version>` section. The release pipeline publishes that section as the GitHub release notes, and refuses to release a version without one.

## Unreleased

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
