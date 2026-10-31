from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .config import Config
from .security import AuthError, PinGuard, RateLimited
from .store import CapacityError, Conflict, NotFound, Store, StoreError


ITEM_RE = re.compile(r"^/api/items/([0-9a-f]{32})$")
AUDIT_RE = re.compile(r"^/api/items/([0-9a-f]{32})/audit$")
CONTENT_RE = re.compile(r"^/api/items/([0-9a-f]{32})/content(?:/(.*))?$")
EVIDENCE_RE = re.compile(r"^/api/items/([0-9a-f]{32})/evidence/([^/]+)$")
MOVIE_EDIT_RE = re.compile(r"^/api/items/([0-9a-f]{32})/movie-edit$")
SHOW_IN_FOLDER_RE = re.compile(r"^/api/items/([0-9a-f]{32})/show-in-folder$")
NOTES_RE = re.compile(r"^/api/items/([0-9a-f]{32})/notes$")
NOTE_SCREENSHOT_RE = re.compile(r"^/api/items/([0-9a-f]{32})/notes/([0-9a-f]{32})\.jpg$")
NOTE_DELETE_RE = re.compile(r"^/api/items/([0-9a-f]{32})/notes/([0-9a-f]{32})/delete$")
NOTE_EDIT_RE = re.compile(r"^/api/items/([0-9a-f]{32})/notes/([0-9a-f]{32})/edit$")
SLICER_RE = re.compile(r"^/api/items/([0-9a-f]{32})/open-in-slicer$")
SLICER_COPY_LIMIT = 512 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class PreviewerServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], config: Config):
        self.config = config
        self.store = Store(config)
        self.pin_guard = PinGuard(
            self.store.database,
            config.pin_hash,
            max_attempts=config.rate_limit_attempts,
            window_seconds=config.rate_limit_window_seconds,
        )
        self.show_in_folder = show_in_file_manager
        self.open_in_slicer = lambda path: launch_slicer(config.slicer_path, path)
        super().__init__(address, PreviewerHandler)


class PreviewerHandler(BaseHTTPRequestHandler):
    server: PreviewerServer

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        try:
            if path == "/api/health":
                self._json({
                    "status": "ok",
                    "service": "previewer",
                    "pin_configured": self.server.pin_guard.configured(),
                })
            elif path == "/api/items":
                state = parse_qs(parsed.query).get("state", [None])[0]
                self._json({"items": [_public_item(item) for item in self.server.store.list(state=state)]})
            elif path == "/api/capacity":
                self._json(self.server.store.capacity())
            elif path == "/api/targets":
                self._json({"targets": [target.__dict__ for target in self.server.config.targets().values()]})
            elif match := ITEM_RE.match(path):
                self._json(_public_item(self.server.store.get(match.group(1))))
            elif match := AUDIT_RE.match(path):
                self._json({"events": self.server.store.audit(match.group(1))})
            elif match := MOVIE_EDIT_RE.match(path):
                self._json(self.server.store.movie_edit(match.group(1)))
            elif match := NOTES_RE.match(path):
                self._json({"notes": [_public_note(note) for note in self.server.store.notes(match.group(1))]})
            elif match := NOTE_SCREENSHOT_RE.match(path):
                self._file(self.server.store.note_screenshot_path(match.group(1), match.group(2)))
            elif match := CONTENT_RE.match(path):
                self._file(self.server.store.content_path(match.group(1), unquote(match.group(2) or "")))
            elif match := EVIDENCE_RE.match(path):
                self._file(self.server.store.evidence_path(match.group(1), unquote(match.group(2))))
            elif path == "/" or path == "/index.html":
                self._file(self.server.config.static_dir / "index.html", cache=False)
            elif path.startswith("/static/"):
                requested = (self.server.config.static_dir / path.removeprefix("/static/")).resolve()
                if not requested.is_relative_to(self.server.config.static_dir.resolve()):
                    raise NotFound("static file not found")
                self._file(requested, cache=requested.suffix not in {".js", ".css"})
            else:
                raise NotFound("route not found")
        except ConnectionError:  # BrokenPipe, ConnectionReset, and ConnectionAborted on Windows
            return
        except Exception as exc:
            self._error(exc)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        try:
            if (match := SLICER_RE.match(parsed.path)) and self.headers.get("Content-Type", "").startswith("model/stl"):
                # PrusaSlicer cannot read glTF, so the viewer sends an STL copy of the model.
                path = self.server.store.save_slicer_copy(match.group(1), self._read_body(SLICER_COPY_LIMIT))
                self.server.open_in_slicer(path)
                self._json({"opened": "stl copy"})
                return
            body = self._read_json()
            if parsed.path == "/api/submit":
                to = body.get("to") if isinstance(body, dict) else None
                replaces = body.get("replaces") if isinstance(body, dict) else None
                # Placing straight into Movies skips approve, which needs the PIN away from this computer.
                if to is not None and not self._direct_local():
                    raise StoreError("to movies works only from this computer")
                # A submission names a file on this computer, so only this computer may submit. Through a proxy,
                # or from a page under another Host (DNS rebinding), it would copy any readable file to read back.
                if not self._direct_local():
                    raise StoreError("submit works only from this computer")
                # A resubmission clears the agent's own older candidate, but only once the new one is ready
                # The older one is checked first so a bad id stages nothing.
                if replaces is not None:
                    self.server.store.check_replaceable(replaces, str(body.get("project_run_id") or "").strip())
                item = self.server.store.submit(body, actor="agent")
                item = self.server.store.verify(item["id"], actor="verifier")
                replaced = None
                if replaces is not None and item["verification_status"] == "ready":
                    self.server.store.replace(replaces, item, actor="agent")
                    replaced = replaces
                if to == "movies" and item["verification_status"] == "ready":
                    item = self.server.store.approve(item["id"], actor="agent", by_agent=True)
                payload = _public_item(item)
                if replaces is not None:
                    payload["replaced"] = replaced
                self._json(payload, HTTPStatus.CREATED)
            elif parsed.path == "/api/human/pin":
                try:
                    self.server.pin_guard.change(
                        self.headers.get("X-Previewer-PIN", ""),
                        body.get("new_pin"),
                        self._client_key(),
                    )
                except ValueError as exc:
                    raise StoreError(str(exc)) from exc
                self._json({"changed": True, "pin_configured": True})
            elif parsed.path == "/api/human/action":
                # No PIN on actions from this desktop: items are copies.
                # Approve and delete from anywhere else, such as a phone or TV through a reverse proxy, need the PIN.
                item_id = body.get("item_id")
                action = body.get("action")
                if not isinstance(item_id, str) or action not in {"approve", "hide", "restore", "delete", "save_movie_edit"}:
                    raise StoreError("invalid human action")
                if action in {"approve", "delete"} and not self._direct_local():
                    pin = self.headers.get("X-Previewer-PIN", "")
                    if not pin:
                        raise AuthError("PIN required")
                    self.server.pin_guard.require(pin, self._client_key())
                if action == "save_movie_edit":
                    result = self.server.store.save_movie_edit(
                        item_id, body.get("edit"), body.get("base_revision"), actor="human"
                    )
                    self._json(result)
                else:
                    result = getattr(self.server.store, action)(item_id, actor="human")
                    self._json(_public_item(result))
            elif match := NOTES_RE.match(parsed.path):
                # Notes are the reviewer's own marks on a movie or 3D model, like hide and restore: no PIN.
                note = self.server.store.add_note(
                    match.group(1), body.get("at_seconds"), body.get("text", ""), _note_screenshot(body.get("screenshot")),
                    view=body.get("view"), actor="human",
                )
                self._json(_public_note(note), HTTPStatus.CREATED)
            elif match := NOTE_EDIT_RE.match(parsed.path):
                note = self.server.store.edit_note(match.group(1), match.group(2), body.get("text"), actor="human")
                self._json(_public_note(note))
            elif match := NOTE_DELETE_RE.match(parsed.path):
                self.server.store.delete_note(match.group(1), match.group(2), actor="human")
                self._json({"deleted": True})
            elif match := SHOW_IN_FOLDER_RE.match(parsed.path):
                # Opens a window on this desktop; the response never carries the path.
                path, location = self.server.store.folder_target(match.group(1))
                self.server.show_in_folder(path)
                self._json({"opened": location})
            elif match := SLICER_RE.match(parsed.path):
                # Opens PrusaSlicer on this desktop, like Open in folder; the response never carries the path.
                path, location = self.server.store.slicer_target(match.group(1))
                self.server.open_in_slicer(path)
                self._json({"opened": location})
            else:
                raise NotFound("route not found")
        except Exception as exc:
            self._error(exc)

    def _read_json(self) -> dict:
        # A cross-site form or text/plain POST needs no preflight; application/json does, and Previewer never grants one.
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise StoreError("request body must be sent as application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise StoreError("invalid Content-Length") from exc
        if length <= 0 or length > 2 * 1024 * 1024:
            raise StoreError("JSON request body is required and limited to 2 MiB")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise StoreError("request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise StoreError("request body must be a JSON object")
        return value

    def _read_body(self, limit: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise StoreError("invalid Content-Length") from exc
        if length <= 0 or length > limit:
            raise StoreError(f"request body is required and limited to {limit // (1024 * 1024)} MiB")
        data = self.rfile.read(length)
        if len(data) != length:
            raise StoreError("request body ended early")
        return data

    def _json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _file(self, path: Path, *, cache: bool = True) -> None:
        if not path.is_file():
            raise NotFound("file not found")
        size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1 if size else -1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes=") and size:
            spec = range_header.removeprefix("bytes=").split("-", 1)
            if spec[0]:
                start = int(spec[0])
            if len(spec) > 1 and spec[1]:
                end = min(int(spec[1]), size - 1)
            if start < 0 or start >= size or end < start:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self._security_headers()
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT
        length = 0 if size == 0 else end - start + 1
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=3600, immutable" if cache else "no-store")
        self.end_headers()
        if self.command == "HEAD" or length == 0:
            return
        # The file is open only while each chunk is read: Windows refuses to move or delete an open file, so a
        # video that streams for minutes would block approve and delete. A file moved mid-stream ends the stream.
        offset, remaining = start, length
        while remaining:
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read(min(1024 * 1024, remaining))
            except OSError:
                return
            if not chunk:
                break
            self.wfile.write(chunk)
            offset += len(chunk)
            remaining -= len(chunk)

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, NotFound):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(exc, (Conflict, CapacityError)):
            status = HTTPStatus.CONFLICT
        elif isinstance(exc, RateLimited):
            status = HTTPStatus.TOO_MANY_REQUESTS
        elif isinstance(exc, AuthError):
            status = HTTPStatus.FORBIDDEN
        elif isinstance(exc, StoreError):
            status = HTTPStatus.BAD_REQUEST
        else:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        payload = {"error": str(exc) if status != HTTPStatus.INTERNAL_SERVER_ERROR else "internal server error"}
        if status == HTTPStatus.FORBIDDEN:
            payload["pin_required"] = True
        self._json(payload, status)

    def _security_headers(self) -> None:
        frame_origins = " ".join(self.server.config.allowed_origins)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self'; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            f"frame-src 'self' {frame_origins}; frame-ancestors 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'",
        )

    def _client_key(self) -> str:
        # Proxied requests all arrive from loopback; one shared bucket keeps their failed PINs from locking out this desktop.
        return self.client_address[0] if self._direct_local() else "proxied"

    def _direct_local(self) -> bool:
        # Tailscale serve connects from loopback but keeps the ts.net Host and adds forwarding and identity headers.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        proxied = any(
            name.lower().startswith(("x-forwarded-", "tailscale-")) or name.lower() in {"forwarded", "x-real-ip"}
            for name in self.headers.keys()
        )
        return self.client_address[0] in LOOPBACK_HOSTS and host in LOOPBACK_HOSTS and not proxied

    def log_message(self, format: str, *args: object) -> None:
        return


def build_server(config: Config, host: str = "127.0.0.1", port: int = 8087) -> PreviewerServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("Previewer may bind only to loopback")
    return PreviewerServer((host, port), config)


def show_in_file_manager(path: Path) -> None:
    """Open the desktop file manager at path, selecting the file where the manager supports it."""
    if sys.platform == "win32":
        # Explorer opens the folder with the file selected. The command line is passed as written,
        # because Explorer misreads /select when the whole argument is quoted.
        subprocess.Popen(f'explorer "{path}"' if path.is_dir() else f'explorer /select,"{path}"')
        return
    method = "ShowFolders" if path.is_dir() else "ShowItems"
    try:
        subprocess.run(
            ["busctl", "--user", "call", "org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
             "org.freedesktop.FileManager1", method, "ass", "1", path.as_uri(), ""],
            check=True, capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        subprocess.Popen(
            ["xdg-open", str(path if path.is_dir() else path.parent)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        return
    try:
        _raise_dolphin((path if path.is_dir() else path.parent).name)
    except (OSError, subprocess.SubprocessError):
        pass  # The folder is open; it just stays behind the browser.


def launch_slicer(slicer: str, path: Path) -> None:
    """Open path in the slicer on this desktop and return without waiting for it."""
    slicer = shutil.which(slicer) or slicer
    if not os.access(slicer, os.X_OK):
        raise Conflict("PrusaSlicer is not installed where Previewer expects it (PREVIEWER_SLICER)")
    subprocess.Popen([slicer, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _raise_dolphin(folder_name: str) -> None:
    """KWin keeps a window opened over D-Bus behind the browser, so ask it to activate the matching Dolphin window."""
    if "KDE" not in os.environ.get("XDG_CURRENT_DESKTOP", ""):
        return
    script = (
        "const wins = workspace.windowList().filter(w => w.resourceClass === 'org.kde.dolphin'"
        f" && w.caption.indexOf({json.dumps(folder_name)}) === 0);"
        "if (wins.length) workspace.activeWindow = wins[wins.length - 1];"
    )

    def kwin(*args: str) -> str:
        return subprocess.run(["qdbus6", "org.kde.KWin", *args], check=True, capture_output=True, text=True, timeout=5).stdout.strip()

    name = f"previewer-raise-{os.getpid()}-{time.monotonic_ns()}"
    with tempfile.NamedTemporaryFile("w", suffix=".js") as handle:
        handle.write(script)
        handle.flush()
        script_id = kwin("/Scripting", "org.kde.kwin.Scripting.loadScript", handle.name, name)
        try:
            kwin(f"/Scripting/Script{script_id}", "org.kde.kwin.Script.run")
        finally:
            kwin("/Scripting", "org.kde.kwin.Scripting.unloadScript", name)


def _note_screenshot(value: object) -> bytes | None:
    if value is None:
        return None
    prefix = "data:image/jpeg;base64,"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise StoreError("screenshot must be a JPEG data URL")
    try:
        return base64.b64decode(value[len(prefix):], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise StoreError("screenshot is not valid base64") from exc


def _public_note(note: dict) -> dict:
    url = f"/api/items/{note['item_id']}/notes/{note['id']}.jpg" if note["has_screenshot"] else None
    return {key: note[key] for key in ("id", "item_id", "at_seconds", "text", "created_at", "view")} | {"screenshot_url": url}


def _public_item(item: dict) -> dict:
    public = dict(item)
    source = public.pop("source", "")
    approved_path = public.get("approved_path")
    public.pop("staged_path", None)
    public.pop("approved_path", None)
    source_name = source if public["kind"] in {"live_url", "novnc"} else Path(source).name
    if public["kind"] == "model3d" and approved_path and Path(approved_path).is_dir():
        entries = list(Path(approved_path).glob("*.gltf"))
        if len(entries) == 1:
            source_name = entries[0].name
    public["source_name"] = source_name
    return public
