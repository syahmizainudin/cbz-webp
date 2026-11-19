# Changelog

Each release has a `## <version>` section. The release pipeline publishes that section as the GitHub release notes, and refuses to release a version without one.

## Unreleased

## 0.5.1

- **Watch folders:** `cbz-webp ui --watch FOLDER` (and `cbzwebp.watch.Watcher` for hosts) queues every archive that appears in the folder once it has stopped changing, converts each version of a file once, and never queues its own output. Watched originals are kept unless `--watch-delete` is given.

## 0.5.0

- **For hosts (harbor): reaching the console from other devices.** `serve()` takes `host` (bind address), `allowed_hosts` (host names accepted besides 127.0.0.1 and localhost), `ssl_context` (HTTPS; the TLS handshake runs in the request's thread) and `trust_proxy` (a reverse proxy on this machine may pass `X-Forwarded-Proto`, `-Host` and `-For`). The Origin check follows the scheme and host actually used. Without them nothing changes: the console stays on 127.0.0.1.
- `current_client()` names the client's address. An API's `get` may return `FileBody(path)` to stream a file in chunks, with single `Range` requests answered 206.
- A request that stops sending is dropped after 120 s, and handshake errors and dropped connections are no longer printed.

## 0.4.2

- Faster checks before an archive is replaced: the written `.cbz.part` is read back in one pass (every entry's CRC) and its WebP pages are decoded on the image threads instead of one at a time. It is still read from disk, and a failure still leaves the original untouched.
- Folder scans stat only `.zip`/`.cbz` files.
- Pages are rotated by their EXIF orientation without an extra copy, and RGB/RGBA pages skip a needless mode conversion.
- Cancel all cancels every job in one step under the queue lock.
- The console polls with one timer and never overlaps requests; it slows to every 5 s in a background tab and updates the stats and job table only when they change.
- `convert_archive` takes `window=` (images in flight on the pool) instead of reading the pool's private worker count; `core.output_path(src)` names the `.cbz` an archive is written to.

## 0.4.1

- Sign-in: the `auth` object may also define `allows(user, method, path)`. When it returns False, a signed-in request gets 403. Hosts use it for permissions on what the server serves itself, such as the folder browser (`/api/dirs`).

## 0.4.0

- The console server can require sign-in, for tools built on it (harbor's accounts):
  - `serve(..., auth=...)` takes an object that says who sent each request and which requests are allowed signed out. Signed-out page requests are sent to the sign-in page, and API requests get 401.
  - `current_user()` names the signed-in user while an API handles a request.
  - An API's `post` may return extra response headers, such as `Set-Cookie`.
  Without `auth`, nothing changes.
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
