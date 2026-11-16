# Changelog

Each release has a `## <version>` section. The release pipeline publishes that section as the GitHub release notes, and refuses to release a version without one.

## Unreleased

- Web console: a **Download** tab with its own queue. It downloads direct file URLs into a chosen folder, sends an API key as a Bearer token, a named header or a query parameter, and passes downloaded `.zip`/`.cbz` files to the convert queue (optional, on by default). The key is never shown in the queue or sent on redirects, and downloads never overwrite existing files.
- Web console: an **Open folder…** button that browses WSL folders, with shortcuts to home and each Windows drive.
- `cbz-webp ui --downloads N` sets how many downloads run at once (default 2).

## 0.1.0

First release.

- `cbz-webp convert` scans folders (subfolders included), queues every `.zip` and `.cbz`, recompresses the images to WebP at the chosen quality and writes `.cbz` files.
- `cbz-webp ui` serves a local web console to add folders, watch progress, cancel, retry, pause and resume.
- The original `.zip` is deleted only after the new `.cbz` is written, reopened and checked; `--keep` keeps it.
- Archives with no images, `.cbz` files that are already all WebP, and a `.zip` with a `.cbz` already beside it are skipped and left untouched.
- Two jobs that write the same `.cbz` (`Name.zip` and `Name.cbz` with `--overwrite`) never run at the same time.
