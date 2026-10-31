from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from .adapters import AdapterError, previewer_item_url
from .config import Config
from .security import hash_pin
from .service import build_server


def _request(base_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            message = json.load(exc).get("error", str(exc))
        except Exception:
            message = str(exc)
        raise SystemExit(f"previewer: {message}") from exc


def _declaration(args: argparse.Namespace) -> dict:
    if args.json:
        return json.loads(Path(args.json).read_text(encoding="utf-8"))
    checks = json.loads(args.checks) if args.checks else []
    value = {
        "project_run_id": args.project_run_id,
        "title": args.title,
        "kind": args.kind,
        "source": str(Path(args.source).expanduser().resolve()) if args.source else args.source,
        "expected_result": args.expected_result,
        "checks": checks,
    }
    if args.target_id:
        value["target_id"] = args.target_id
    if args.to:
        value["to"] = args.to
    if args.replaces:
        value["replaces"] = args.replaces
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="previewer")
    parser.add_argument("--url", default=os.environ.get("PREVIEWER_URL", "http://127.0.0.1:8087"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit", help="submit and verify a candidate")
    submit.add_argument("source", nargs="?")
    submit.add_argument("--json", help="declaration JSON file")
    submit.add_argument("--project-run-id")
    submit.add_argument("--title")
    submit.add_argument("--kind", choices=sorted({
        "image", "video", "audio", "markdown", "report", "pdf", "log", "subtitle", "file",
        "html", "live_url", "novnc", "model3d", "blend", "apk", "movie_project",
    }))
    submit.add_argument("--expected-result")
    submit.add_argument("--checks", help="JSON list of declarative checks")
    submit.add_argument("--target-id")
    submit.add_argument("--to", choices=["movies"], help="place a verified video, audio or movie project straight into Movies")
    submit.add_argument("--replaces", metavar="ITEM_ID",
                        help="delete this older candidate from the same --project-run-id once the new one is ready")

    status = subparsers.add_parser("status", help="read an item or list candidates")
    status.add_argument("item_id", nargs="?")

    notes = subparsers.add_parser("notes", help="print reviewer notes on a movie or 3D model")
    notes.add_argument("item_id")
    notes.add_argument("--out", help="existing folder to save each note's screenshot into")

    open_command = subparsers.add_parser("open", help="open Previewer or an item")
    open_command.add_argument("item_id", nargs="?")
    open_command.add_argument("--browser", action="store_true", help="launch a browser tab (off by default)")
    open_command.add_argument("--no-browser", action="store_true", help="do not launch a browser (default)")

    serve = subparsers.add_parser("serve", help="run the loopback-only service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8087)

    subparsers.add_parser("hash-pin", help="print a scrypt hash for a human-entered PIN")

    args = parser.parse_args(argv)
    if args.command == "submit":
        declaration = _declaration(args)
        missing = [key for key in ("project_run_id", "title", "kind", "source", "expected_result") if not declaration.get(key)]
        if missing:
            parser.error("missing declaration fields: " + ", ".join(missing))
        result = _request(args.url, "POST", "/api/submit", declaration)
        print(json.dumps(result, indent=2))
        return 0 if result["verification_status"] == "ready" else 2
    if args.command == "status":
        path = f"/api/items/{args.item_id}" if args.item_id else "/api/items?state=candidate"
        print(json.dumps(_request(args.url, "GET", path), indent=2))
        return 0
    if args.command == "notes":
        out = Path(args.out).expanduser().resolve() if args.out else None
        if out and not out.is_dir():
            parser.error(f"--out folder does not exist: {out}")
        item = _request(args.url, "GET", f"/api/items/{args.item_id}")
        notes = _request(args.url, "GET", f"/api/items/{args.item_id}/notes")["notes"]
        for note in notes:
            if not note["screenshot_url"]:
                continue
            url = args.url.rstrip("/") + note["screenshot_url"]
            note["screenshot_url"] = url
            if out:
                target = out / f"{note['at_seconds']:09.3f}s-{note['id'][:8]}.jpg"
                with urllib.request.urlopen(url, timeout=60) as response:
                    target.write_bytes(response.read())
                note["screenshot_file"] = str(target)
        print(json.dumps({"item_id": item["id"], "title": item["title"], "notes": notes}, indent=2, ensure_ascii=False))
        return 0
    if args.command == "open":
        try:
            target = previewer_item_url(args.url, args.item_id)
        except AdapterError as exc:
            raise SystemExit(f"previewer: {exc}") from exc
        return _open_browser(target, skip=not _want_browser(args))
    if args.command == "hash-pin":
        first = getpass.getpass("PIN (4 to 32 digits): ")
        second = getpass.getpass("Confirm PIN: ")
        if first != second:
            raise SystemExit("previewer: PIN values did not match")
        print(hash_pin(first))
        return 0
    if args.command == "serve":
        server = build_server(Config.from_env(), args.host, args.port)
        print(f"Previewer listening on http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()
        return 0
    return 1


def _want_browser(args: argparse.Namespace) -> bool:
    if getattr(args, "no_browser", False):
        return False
    if getattr(args, "browser", False):
        return True
    return os.environ.get("PREVIEWER_OPEN_BROWSER", "").strip().lower() in {"1", "true", "yes", "on"}


def _open_browser(target: str, *, skip: bool = False) -> int:
    print(target)
    if skip:
        return 0
    if not webbrowser.open(target):
        raise SystemExit("previewer: no browser accepted the open request")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
