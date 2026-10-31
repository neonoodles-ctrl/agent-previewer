from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sqlite3
import statistics
import struct
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image, ImageStat

from previewer.adapters import previewer_item_url
from previewer.cli import main
from previewer.config import Config
from previewer.database import Database
from previewer.security import hash_pin
from previewer.service import build_server

from model_fixtures import binary_stl, checker_png, cube_glb, cube_triangles, draco_glb, three_mf


TEST_PIN = "123456"
EVIDENCE_DIR = Path(os.environ["PREVIEWER_E2E_OUT"]) if os.environ.get("PREVIEWER_E2E_OUT") else None


class EndToEndProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(
            data_dir=self.root / "data",
            pin_hash=hash_pin(TEST_PIN),
            volume_limit_bytes=8 * 1024 * 1024,
            rate_limit_attempts=2,
            rate_limit_window_seconds=60,
        )
        self.server = build_server(self.config, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def patterned_image(self, name="pattern.png") -> Path:
        path = self.root / name
        image = Image.new("RGB", (64, 64))
        pixels = image.load()
        for y in range(64):
            for x in range(64):
                pixels[x, y] = (230, 70, 60) if (x // 8 + y // 8) % 2 else (30, 90, 210)
        image.save(path)
        return path

    def declaration(self, source: Path | str, kind="image", **extra) -> dict:
        return {
            "project_run_id": extra.pop("project_run_id", "project/e2e"),
            "title": extra.pop("title", "E2E sample"),
            "kind": kind,
            "source": str(source),
            "expected_result": extra.pop("expected_result", "A visible valid sample"),
            **extra,
        }

    def request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + path, data=data, method=method, headers=request_headers)
        try:
            with urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode()) if response.headers.get("Content-Type", "").startswith("application/json") else response.read()
                return response.status, dict(response.headers), body
        except HTTPError as exc:
            try:
                raw = exc.read()
                body = json.loads(raw.decode())
            except Exception:
                body = {"error": str(exc)}
            finally:
                exc.close()
            return exc.code, dict(exc.headers), body

    def submit(self, declaration: dict) -> dict:
        status, _, body = self.request("POST", "/api/submit", declaration)
        self.assertIn(status, {201, 400, 409}, body)
        return status, body

    def human(self, item_id: str, action: str, pin: str | None, extra: dict | None = None) -> tuple[int, dict]:
        headers = {}
        if pin is not None:
            headers["X-Previewer-PIN"] = pin
        payload = {"item_id": item_id, "action": action, **(extra or {})}
        status, _, body = self.request("POST", "/api/human/action", payload, headers)
        return status, body

    def audit_events(self, item_id: str) -> list[dict]:
        status, _, body = self.request("GET", f"/api/items/{item_id}/audit")
        self.assertEqual(status, 200, body)
        return body["events"]

    def test_submit_cannot_self_approve_and_actions_need_no_pin(self):
        image = self.patterned_image()
        status, item = self.submit(self.declaration(image, state="approved", verification_status="ready", actor="human"))
        self.assertEqual(status, 201, item)
        self.assertEqual(item["state"], "candidate")
        self.assertEqual(item["verification_status"], "ready")
        item_id = item["id"]

        status, _, listed = self.request("GET", f"/api/items/{item_id}")
        self.assertEqual(status, 200)
        self.assertEqual(listed["state"], "candidate")
        self.assertNotIn("staged_path", listed)
        self.assertNotIn("approved_path", listed)
        self.assertNotIn("source", listed)

        delete_status, _, delete_body = self.request("DELETE", f"/api/items/{item_id}")
        self.assertEqual(delete_status, 501)
        approve_status, _, approve_body = self.request("POST", "/api/approve", {"item_id": item_id})
        self.assertEqual(approve_status, 404, approve_body)
        events = [event["event"] for event in self.audit_events(item_id)]
        self.assertEqual(events, ["submitted", "verification_ready"])
        actors = [event["actor"] for event in self.audit_events(item_id)]
        self.assertEqual(actors, ["agent", "verifier"])

        status, body = self.human(item_id, "delete", pin=None)
        self.assertEqual(status, 200, body)

    def test_show_in_folder_opens_original_without_exposing_paths(self):
        shown = []
        self.server.show_in_folder = shown.append
        source = self.patterned_image()
        status, item = self.submit(self.declaration(source))
        self.assertEqual(status, 201, item)
        status, _, body = self.request("POST", f"/api/items/{item['id']}/show-in-folder", {})
        self.assertEqual((status, body), (200, {"opened": "original"}))
        self.assertEqual(shown, [source.resolve()])
        source.unlink()
        status, _, body = self.request("POST", f"/api/items/{item['id']}/show-in-folder", {})
        self.assertEqual((status, body), (200, {"opened": "copy"}))
        self.assertNotIn(str(self.root), json.dumps(body))

    def test_corrupt_and_blank_submissions_fail_over_http(self):
        corrupt = self.root / "broken.png"
        corrupt.write_bytes(b"not an image")
        status, item = self.submit(self.declaration(corrupt, title="Corrupt"))
        self.assertEqual(status, 201, item)
        self.assertEqual(item["verification_status"], "rejected")
        self.assertEqual(item["state"], "candidate")
        self.assertFalse(item["receipt"]["human_approval"])
        self.assertFalse(item["receipt"]["publicly_published"])

        blank = self.root / "blank.png"
        Image.new("RGB", (80, 80), "white").save(blank)
        status, item = self.submit(self.declaration(blank, title="Blank"))
        self.assertEqual(status, 201, item)
        self.assertEqual(item["verification_status"], "rejected")

        empty = self.root / "empty.md"
        empty.write_text("   \n", encoding="utf-8")
        status, item = self.submit(self.declaration(empty, "markdown", title="Blank markdown"))
        self.assertEqual(status, 201, item)
        self.assertEqual(item["verification_status"], "rejected")

        status, body = self.human(item["id"], "approve", TEST_PIN)
        self.assertEqual(status, 409, body)

    def test_approved_bytes_cannot_change(self):
        image = self.patterned_image("immutable.png")
        status, item = self.submit(self.declaration(image))
        self.assertEqual(status, 201, item)
        item_id = item["id"]
        original = hashlib.sha256(image.read_bytes()).hexdigest()
        self.assertEqual(item["sha256"], original)

        status, approved = self.human(item_id, "approve", TEST_PIN)
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(approved["sha256"], original)

        content_status, headers, body = self.request("GET", f"/api/items/{item_id}/content")
        self.assertEqual(content_status, 200)
        self.assertEqual(hashlib.sha256(body).hexdigest(), original)
        self.assertIn("nosniff", headers.get("X-Content-Type-Options", ""))

        approved_files = [path for path in (self.config.approved_dir / item_id).rglob("*") if path.is_file()]
        self.assertEqual(len(approved_files), 1)
        target = approved_files[0]
        self.assertEqual(target.stat().st_mode & 0o777, 0o444)
        target.chmod(0o600)
        target.write_bytes(b"changed-approved-bytes")
        tamper_status, _, tamper_body = self.request("GET", f"/api/items/{item_id}/content")
        self.assertEqual(tamper_status, 409, tamper_body)
        self.assertIn("integrity", tamper_body["error"])

    def test_pin_audit_and_capacity_limits(self):
        image = self.patterned_image("audited.png")
        status, item = self.submit(self.declaration(image, title="PIN audit sample"))
        self.assertEqual(status, 201, item)
        item_id = item["id"]

        status, _, still = self.request("GET", f"/api/items/{item_id}")
        self.assertEqual(status, 200)
        self.assertEqual(still["state"], "candidate")

        status, approved = self.human(item_id, "approve", None)
        self.assertEqual(status, 200, approved)
        status, hidden = self.human(item_id, "hide", TEST_PIN)
        self.assertEqual(status, 200, hidden)
        status, restored = self.human(item_id, "restore", TEST_PIN)
        self.assertEqual(status, 200, restored)
        self.human(item_id, "hide", TEST_PIN)
        status, deleted = self.human(item_id, "delete", TEST_PIN)
        self.assertEqual(status, 200, deleted)
        self.assertEqual(deleted["state"], "deleted")

        events = self.audit_events(item_id)
        self.assertEqual(
            [event["event"] for event in events],
            ["submitted", "verification_ready", "approved", "hidden", "restored", "hidden", "deleted"],
        )
        self.assertEqual(events[0]["actor"], "agent")
        self.assertEqual(events[1]["actor"], "verifier")
        self.assertTrue(all(event["actor"] == "human" for event in events[2:]))
        database = Database(self.config.db_path)
        with database.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM audit_events WHERE item_id = ?", (item_id,))

        small_root = self.root / "cap"
        small_root.mkdir()
        small_config = Config(data_dir=small_root / "data", volume_limit_bytes=1_000_000)
        small_server = build_server(small_config, "127.0.0.1", 0)
        small_thread = threading.Thread(target=small_server.serve_forever, daemon=True)
        small_thread.start()
        try:
            small_base = f"http://127.0.0.1:{small_server.server_address[1]}"
            large = small_root / "large.bin"
            large.write_bytes(os.urandom(780_000))
            request = Request(
                small_base + "/api/submit",
                data=json.dumps(self.declaration(large, "file", title="Capacity warning")).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=60) as response:
                warned = json.load(response)
            self.assertEqual(warned["verification_status"], "ready")
            self.assertTrue(warned["receipt"]["capacity"]["warning"])
            extra = small_root / "extra.bin"
            extra.write_bytes(os.urandom(160_000))
            extra_request = Request(
                small_base + "/api/submit",
                data=json.dumps(self.declaration(extra, "file", title="Capacity reject")).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as caught:
                urlopen(extra_request, timeout=60)
            self.assertEqual(caught.exception.code, 409)
            caught.exception.close()
        finally:
            small_server.shutdown()
            small_server.server_close()
            small_thread.join(timeout=5)

    def test_pin_can_be_changed_over_http(self):
        status, _, body = self.request("POST", "/api/human/pin", {"new_pin": "1234"})
        self.assertEqual(status, 403, body)
        status, _, body = self.request(
            "POST",
            "/api/human/pin",
            {"new_pin": "1234"},
            {"X-Previewer-PIN": TEST_PIN},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["changed"])

        status, item = self.submit(self.declaration(self.patterned_image("new-pin.png")))
        self.assertEqual(status, 201, item)
        status, body = self.human(item["id"], "approve", "1234")
        self.assertEqual(status, 200, body)
        with self.server.store.database.connect() as connection:
            audit = connection.execute(
                "SELECT event, actor FROM audit_events WHERE item_id = 'system'"
            ).fetchall()
        self.assertEqual([tuple(event) for event in audit], [("pin_changed", "human")])

    def test_movie_edit_notes_are_revisioned(self):
        manifest = self.root / "agent-edit.movie.json"
        manifest.write_text(json.dumps({
            "version": 1,
            "timeline": {
                "duration_seconds": 45,
                "frame_rate": 25,
                "width": 1920,
                "height": 1080,
                "color_space": "Rec. 709",
            },
            "clips": [],
            "markers": [],
        }), encoding="utf-8")
        status, item = self.submit(self.declaration(manifest, "movie_project", title="Agent edit plan"))
        self.assertEqual(status, 201, item)
        status, approved = self.human(item["id"], "approve", TEST_PIN)
        self.assertEqual(status, 200, approved)

        edit = {
            "version": 1,
            "title": "Agent edit plan",
            "duration_seconds": 45,
            "frame_rate": 25,
            "resolution": {"width": 1920, "height": 1080},
            "color_space": "Rec. 709",
            "annotations": [{
                "id": "music-cue-1",
                "type": "music",
                "start_seconds": 4,
                "end_seconds": 14,
                "label": "Music enters",
                "notes": "Low pulse, then build.",
            }],
        }
        status, saved = self.human(item["id"], "save_movie_edit", None, {
            "base_revision": 0, "edit": edit,
        })
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["revision"], 1)
        status, _, loaded = self.request("GET", f"/api/items/{item['id']}/movie-edit")
        self.assertEqual(status, 200, loaded)
        self.assertEqual(loaded["document"]["annotations"][0]["label"], "Music enters")
        self.assertEqual([event["event"] for event in self.audit_events(item["id"])][-1], "movie_edit_saved")

    def test_proxied_approve_and_delete_need_pin(self):
        ids = []
        for side in (24, 32):
            path = self.root / f"tv-{side}.png"
            Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(path)
            status, item = self.submit(self.declaration(path, title=f"tv-{side}"))
            self.assertEqual(status, 201, item)
            ids.append(item["id"])
        tailnet = {"Host": "previewer.example.ts.net:8087", "X-Forwarded-For": "100.64.0.9", "Tailscale-User-Login": "tv@example.com"}
        variants = [
            tailnet,
            {"X-Forwarded-For": "100.64.0.9"},
            {"Host": "previewer.example.ts.net:8087"},
            {"Tailscale-User-Login": "tv@example.com"},
        ]
        for headers in variants:
            for action in ("approve", "delete"):
                status, _, body = self.request("POST", "/api/human/action", {"item_id": ids[0], "action": action}, headers)
                self.assertEqual((status, body.get("pin_required")), (403, True), (headers, action, body))
        # A missing PIN is not a failed attempt; rate_limit_attempts is 2 here.
        status, _, body = self.request("POST", "/api/human/action", {"item_id": ids[0], "action": "delete"}, {**tailnet, "X-Previewer-PIN": TEST_PIN})
        self.assertEqual(status, 200, body)

        status, _, body = self.request("POST", "/api/human/action", {"item_id": ids[1], "action": "approve"}, {**tailnet, "X-Previewer-PIN": "0000"})
        self.assertEqual((status, body["error"]), (403, "invalid PIN"))
        status, _, body = self.request("POST", "/api/human/action", {"item_id": ids[1], "action": "approve"}, {**tailnet, "X-Previewer-PIN": TEST_PIN})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "approved")
        status, _, body = self.request("POST", "/api/human/action", {"item_id": ids[1], "action": "hide"}, tailnet)
        self.assertEqual(status, 200, body)

        for _ in range(2):
            self.request("POST", "/api/human/action", {"item_id": ids[1], "action": "delete"}, {**tailnet, "X-Previewer-PIN": "0000"})
        status, _, body = self.request("POST", "/api/human/action", {"item_id": ids[1], "action": "delete"}, {**tailnet, "X-Previewer-PIN": TEST_PIN})
        self.assertEqual(status, 429, body)
        # This desktop is a separate bucket and needs no PIN.
        status, body = self.human(ids[1], "delete", pin=None)
        self.assertEqual(status, 200, body)

    def submit_noise(self, title: str, side: int) -> str:
        path = self.root / f"{title}.png"
        Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(path)
        status, item = self.submit(self.declaration(path, title=title))
        self.assertEqual(status, 201, item)
        return item["id"]

    def test_remote_browser_asks_for_pin_once(self):
        for title, side in (("First", 24), ("Second", 32), ("Third", 40)):
            self.submit_noise(title, side)
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            context = browser.new_context(viewport={"width": 960, "height": 540}, extra_http_headers={"X-Forwarded-For": "100.64.0.9"})
            page = context.new_page()
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            page.click("[data-tab=review]")
            page.locator("#item-list .item-card", has_text="Third").click()
            page.get_by_role("button", name="Delete candidate").click()
            expect(page.locator("#pin-dialog")).to_be_visible()
            page.locator("#cancel-action-pin").click()
            expect(page.locator("#pin-dialog")).to_be_hidden()
            self.assertEqual(page.locator("#item-list .item-card").count(), 3)

            page.get_by_role("button", name="Delete candidate").click()
            page.fill("#action-pin", "0000")
            page.get_by_role("button", name="OK").click()
            expect(page.locator("#pin-dialog-error")).to_have_text("invalid PIN")
            page.fill("#action-pin", TEST_PIN)
            page.get_by_role("button", name="OK").click()
            expect(page.locator("#item-list .item-card")).to_have_count(2)

            page.get_by_label("Select First").check()
            page.get_by_label("Select Second").check()
            page.click("#delete-selected")
            expect(page.locator("#item-list .item-card")).to_have_count(0)
            self.assertFalse(page.locator("#pin-dialog").is_visible())
            browser.close()
        _, _, listed = self.request("GET", "/api/items")
        self.assertEqual(listed["items"], [])

    def test_arrow_keys_move_between_list_and_detail(self):
        for title, side in (("One", 24), ("Two", 32), ("Three", 40)):
            self.submit_noise(title, side)
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            page = browser.new_page(viewport={"width": 960, "height": 540})
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            page.click("[data-tab=review]")
            page.locator("#sort").focus()
            cards = page.locator("#item-list .item-card")
            page.keyboard.press("ArrowDown")
            expect(cards.nth(0)).to_be_focused()
            page.keyboard.press("ArrowDown")
            expect(cards.nth(1)).to_be_focused()
            page.keyboard.press("Enter")
            expect(page.locator("#detail-title")).to_have_text("Two")
            page.keyboard.press("ArrowRight")
            expect(page.locator("#detail-actions button").first).to_be_focused()
            page.keyboard.press("ArrowRight")
            expect(page.locator("#detail-actions button").nth(1)).to_be_focused()
            page.keyboard.press("ArrowLeft")
            page.keyboard.press("ArrowLeft")
            expect(cards.nth(1)).to_be_focused()
            page.keyboard.press("ArrowLeft")
            box = page.get_by_label("Select Two")
            expect(box).to_be_focused()
            page.keyboard.press("Enter")
            expect(box).to_be_checked()
            expect(page.locator("#delete-selected")).to_have_text("Delete 1 selected")
            page.keyboard.press("ArrowUp")
            expect(page.get_by_label("Select Three")).to_be_focused()
            if EVIDENCE_DIR:
                EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
                (EVIDENCE_DIR / "tv-focus.png").write_bytes(page.screenshot(animations="disabled"))
            browser.close()

    def test_inbox_scroll_buttons_move_the_list(self):
        for index in range(12):
            self.submit_noise(f"Scroll {index:02d}", 16 + index)
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            page = browser.new_page(viewport={"width": 960, "height": 540})
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            self.assertFalse(page.locator("#scroll-down").is_visible())
            page.click("[data-tab=review]")
            scroll_top = lambda: page.locator("#item-list").evaluate("(node) => node.scrollTop")

            def settle():
                previous = None
                for _ in range(40):
                    current = scroll_top()
                    if current == previous:
                        return current
                    previous = current
                    page.wait_for_timeout(50)
                return current

            self.assertEqual(scroll_top(), 0)
            page.click("#scroll-down")
            first = settle()
            self.assertGreater(first, 0)
            page.click("#scroll-down")
            self.assertGreater(settle(), first)
            page.click("#scroll-up")
            page.click("#scroll-up")
            page.click("#scroll-up")
            self.assertEqual(settle(), 0)
            expect(page.locator("#item-list .item-card").first).to_be_in_viewport()
            browser.close()

    def test_switching_videos_stops_the_old_download(self):
        import subprocess

        ids = {}
        for title, seconds, rate in (("Alpha", 10, "4M"), ("Bravo", 2, "200k")):
            path = self.root / f"{title}.mp4"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", str(seconds),
                            "-c:v", "libx264", "-b:v", rate, "-pix_fmt", "yuv420p", str(path)], check=True)
            status, item = self.submit(self.declaration(path, kind="video", title=title))
            self.assertEqual(status, 201, item)
            ids[title] = item["id"]
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            page = browser.new_page(viewport={"width": 960, "height": 540})
            cdp = page.context.new_cdp_session(page)
            cdp.send("Network.enable")
            # Slow enough that Alpha (about 5 MB) cannot finish downloading during the test.
            cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 0, "downloadThroughput": 500_000, "uploadThroughput": 500_000})
            open_requests = {}
            cdp.on("Network.requestWillBeSent", lambda event: f"/api/items/{ids['Alpha']}/content" in event["request"]["url"] and open_requests.__setitem__(event["requestId"], event["request"]["url"]))
            cdp.on("Network.loadingFinished", lambda event: open_requests.pop(event["requestId"], None))
            cdp.on("Network.loadingFailed", lambda event: open_requests.pop(event["requestId"], None))
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            page.click("[data-tab=review]")
            page.locator("#item-list .item-card", has_text="Alpha").click()
            page.wait_for_timeout(1500)
            self.assertTrue(open_requests, "Alpha should still be downloading before the switch")
            page.locator("#item-list .item-card", has_text="Bravo").click()
            page.wait_for_timeout(2000)
            self.assertEqual(open_requests, {}, "Alpha kept downloading after switching to Bravo")
            self.assertEqual(page.locator("#preview video").count(), 1)
            browser.close()

    def test_movie_editor_redraw_keeps_the_playhead(self):
        first = self.approved_video("Shot A", seconds=20)
        second = self.approved_video("Shot B", seconds=20)
        manifest = self.root / "two-shots.json"
        manifest.write_text(json.dumps({"version": 1, "title": "Two shots", "timeline": {"duration_seconds": 20, "frame_rate": 30, "width": 320, "height": 180},
                                        "clips": [{"item_id": first["id"], "label": "A", "start_seconds": 0, "end_seconds": 10},
                                                  {"item_id": second["id"], "label": "B", "start_seconds": 10, "end_seconds": 20}]}))
        status, project = self.submit(self.declaration(manifest, kind="movie_project", title="Two shots"))
        self.assertEqual(status, 201, project)
        self.assertEqual(self.human(project["id"], "approve", None)[0], 200)
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            page.click("[data-tab=movies]")
            page.locator("#item-list .item-card", has_text="Two shots").click()
            expect(page.locator("#preview video")).to_have_count(1)
            page.wait_for_timeout(1000)
            forward = page.get_by_role("button", name="Move forward five seconds")
            forward.click()
            expect(page.locator(".movie-time")).to_have_text("00:00:05:00 / 00:00:20:00")
            # Clip B uses another video, so the editor redraws and stops the old video, which resets it to 0.
            page.locator(".lane-clip .timeline-marker", has_text="B").click()
            page.wait_for_timeout(500)
            forward.click()
            expect(page.locator(".movie-time")).to_have_text("00:00:15:00 / 00:00:20:00")
            browser.close()

    def make_media(self, name: str, seconds: int = 2, picture: str = "testsrc2=size=320x180:rate=30") -> Path:
        import subprocess

        path = self.root / name
        if path.suffix == ".m4a":
            source = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
        else:
            source = ["-f", "lavfi", "-i", picture, "-t", str(seconds), "-pix_fmt", "yuv420p"]
        subprocess.run(["ffmpeg", "-loglevel", "error", *source, str(path)], check=True)
        return path

    def approved_video(self, title: str, seconds: int = 4, picture: str = "testsrc2=size=320x180:rate=30") -> dict:
        status, item = self.submit(self.declaration(self.make_media(f"{title}.mp4", seconds, picture), kind="video", title=title))
        self.assertEqual(status, 201, item)
        status, approved = self.human(item["id"], "approve", None)
        self.assertEqual(status, 200, approved)
        return approved

    def jpeg_data_url(self, size=(64, 36), colour=(20, 40, 200)) -> tuple[str, bytes]:
        buffer = io.BytesIO()
        Image.new("RGB", size, colour).save(buffer, "JPEG")
        raw = buffer.getvalue()
        return "data:image/jpeg;base64," + base64.b64encode(raw).decode(), raw

    def test_movie_notes_save_list_serve_and_delete(self):
        movie = self.approved_video("Notes clip")
        notes_path = f"/api/items/{movie['id']}/notes"
        data_url, raw = self.jpeg_data_url()
        status, _, note = self.request("POST", notes_path, {"at_seconds": 1.5, "text": "Hand clips the table", "screenshot": data_url})
        self.assertEqual(status, 201, note)
        self.assertEqual((note["at_seconds"], note["text"]), (1.5, "Hand clips the table"))
        self.assertEqual(note["screenshot_url"], f"{notes_path}/{note['id']}.jpg")
        status, _, plain = self.request("POST", notes_path, {"at_seconds": 0.5, "text": "No picture needed"})
        self.assertEqual((status, plain["screenshot_url"]), (201, None))
        listed = self.request("GET", notes_path)[2]["notes"]
        self.assertEqual([value["text"] for value in listed], ["No picture needed", "Hand clips the table"])
        with urlopen(self.base + note["screenshot_url"]) as response:
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")
            self.assertEqual(response.read(), raw)

        status, image = self.submit(self.declaration(self.patterned_image()))
        self.assertEqual(status, 201, image)
        for path, payload, expected in [
            (f"/api/items/{image['id']}/notes", {"at_seconds": 1, "text": "x"}, 409),
            (notes_path, {"at_seconds": 1, "text": "x", "screenshot": "data:image/png;base64,iVBORw0KGgo="}, 400),
            (notes_path, {"at_seconds": 1, "text": "   "}, 400),
            (notes_path, {"at_seconds": -1, "text": "x"}, 400),
        ]:
            status, _, body = self.request("POST", path, payload)
            self.assertEqual(status, expected, (payload, body))

        status, _, body = self.request("POST", f"{notes_path}/{note['id']}/delete", {})
        self.assertEqual((status, body), (200, {"deleted": True}))
        self.assertEqual([value["id"] for value in self.request("GET", notes_path)[2]["notes"]], [plain["id"]])
        self.assertFalse((self.config.data_dir / "notes" / movie["id"] / f"{note['id']}.jpg").exists())
        events = [event["event"] for event in self.request("GET", f"/api/items/{movie['id']}/audit")[2]["events"]]
        self.assertEqual((events.count("note_added"), events.count("note_deleted")), (2, 1))

        status, _, edited = self.request("POST", f"{notes_path}/{plain['id']}/edit", {"text": "  Picture needed after all  "})
        self.assertEqual((status, edited["text"], edited["at_seconds"]), (200, "Picture needed after all", 0.5))
        self.assertEqual([value["text"] for value in self.request("GET", notes_path)[2]["notes"]], ["Picture needed after all"])
        for path, payload, expected in [
            (f"{notes_path}/{plain['id']}/edit", {"text": "   "}, 400),
            (f"{notes_path}/{plain['id']}/edit", {"text": "x" * 2001}, 400),
            (f"{notes_path}/{note['id']}/edit", {"text": "gone"}, 404),
        ]:
            status, _, body = self.request("POST", path, payload)
            self.assertEqual(status, expected, (path, body))
        events = [event["event"] for event in self.request("GET", f"/api/items/{movie['id']}/audit")[2]["events"]]
        self.assertEqual(events.count("note_edited"), 1)

        self.assertEqual(self.request("POST", notes_path, {"at_seconds": 2, "text": "kept", "screenshot": data_url})[0], 201)
        self.assertEqual(self.human(movie["id"], "hide", None)[0], 200)
        self.assertEqual(self.human(movie["id"], "delete", None)[0], 200)
        self.assertFalse((self.config.data_dir / "notes" / movie["id"]).exists())
        self.assertEqual(self.request("GET", notes_path)[0], 404)

    def test_note_button_saves_a_marked_frame_at_the_playhead(self):
        self.approved_video("Monitor clip", seconds=10, picture="color=c=0x2040c0:size=320x180:rate=30,drawbox=x=250:y=110:w=60:h=60:color=white:t=fill")
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            page.click("[data-tab=movies]")
            page.locator("#item-list .item-card", has_text="Monitor clip").click()
            expect(page.locator("#preview video")).to_have_count(1)
            for name in ("Save edit", "Mark playhead", "Add to timeline", "Cue"):
                expect(page.get_by_role("button", name=name, exact=True)).to_have_count(0)
            expect(page.locator(".lane-cut")).to_have_count(0)
            page.wait_for_timeout(1000)
            page.get_by_role("button", name="Move forward five seconds").click()

            page.keyboard.press("n")
            expect(page.locator(".note-layer")).to_have_count(1)
            page.keyboard.press("Escape")
            expect(page.locator(".note-layer")).to_have_count(0)

            page.get_by_role("button", name="Add a note at this frame").click()
            canvas = page.locator(".note-canvas")
            expect(canvas).to_be_visible()
            box = canvas.bounding_box()
            # The frame is 16:9 inside the canvas box; work out where it sits so the drag lands on it.
            scale = min(box["width"] / 320, box["height"] / 180)
            left = box["x"] + (box["width"] - 320 * scale) / 2
            top = box["y"] + (box["height"] - 180 * scale) / 2
            page.mouse.move(left + 96 * scale, top + 54 * scale)
            page.mouse.down()
            page.mouse.move(left + 192 * scale, top + 108 * scale, steps=6)
            page.mouse.up()
            page.get_by_role("textbox", name="Note").fill("Sky flickers")
            page.keyboard.press("Enter")
            expect(page.locator(".note-layer")).to_have_count(0)
            expect(page.locator(".note-row")).to_have_count(1)
            expect(page.locator(".note-row")).to_contain_text("Sky flickers")
            expect(page.locator(".note-row .note-time")).to_have_text("00:00:05:00")
            expect(page.locator(".lane-note .note-dot")).to_have_count(1)
            item_id = page.locator("#item-list .item-card.active").get_attribute("data-item-id")
            notes = self.request("GET", f"/api/items/{item_id}/notes")[2]["notes"]
            self.assertEqual(len(notes), 1)
            self.assertAlmostEqual(notes[0]["at_seconds"], 5.0, places=2)
            with urlopen(self.base + notes[0]["screenshot_url"]) as response:
                shot = Image.open(io.BytesIO(response.read())).convert("RGB")
            self.assertEqual(shot.size, (320, 180))
            red = lambda pixel: pixel[0] > 150 and pixel[2] < 130
            self.assertTrue(red(shot.getpixel((144, 54))), shot.getpixel((144, 54)))
            self.assertFalse(red(shot.getpixel((144, 81))), shot.getpixel((144, 81)))
            self.assertFalse(red(shot.getpixel((20, 20))), shot.getpixel((20, 20)))
            if EVIDENCE_DIR:
                EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
                (EVIDENCE_DIR / "movie-note.png").write_bytes(page.screenshot(animations="disabled"))

            page.get_by_role("button", name="Edit note at 0:05").click()
            box = page.get_by_role("textbox", name="Edit note")
            expect(box).to_have_value("Sky flickers")
            box.fill("Sky flickers twice")
            box.press("Enter")
            expect(page.locator(".note-row .note-body")).to_have_text("Sky flickers twice")
            self.assertEqual(self.request("GET", f"/api/items/{item_id}/notes")[2]["notes"][0]["text"], "Sky flickers twice")
            page.get_by_role("button", name="Edit note at 0:05").click()
            page.get_by_role("textbox", name="Edit note").fill("thrown away")
            page.keyboard.press("Escape")
            expect(page.locator(".note-row .note-body")).to_have_text("Sky flickers twice")
            expect(page.get_by_role("textbox", name="Edit note")).to_have_count(0)

            page.get_by_role("button", name="Delete note at 0:05").click()
            expect(page.locator(".note-row")).to_have_count(0)
            self.assertEqual(self.request("GET", f"/api/items/{item_id}/notes")[2]["notes"], [])
            browser.close()

    def test_timeline_clips_alternate_shades_and_jump_on_click(self):
        video = self.approved_video("Shades film", seconds=12)
        clips = [{"item_id": video["id"], "label": f"Shot {index + 1}", "start_seconds": index * 2, "end_seconds": index * 2 + 2} for index in range(6)]
        manifest = self.root / "shades.json"
        manifest.write_text(json.dumps({"version": 1, "title": "Shades", "timeline": {"duration_seconds": 12, "frame_rate": 24, "width": 320, "height": 180}, "clips": clips}))
        status, project = self.submit(self.declaration(manifest, kind="movie_project", title="Shades project"))
        self.assertEqual(status, 201, project)
        self.assertEqual(self.human(project["id"], "approve", None)[0], 200)
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            page.click("[data-tab=movies]")
            page.locator("#item-list .item-card", has_text="Shades project").click()
            markers = page.locator(".lane-clip .timeline-marker")
            expect(markers).to_have_count(6)
            shades = [next(name for name in markers.nth(index).get_attribute("class").split() if name.startswith("shade-")) for index in range(6)]
            self.assertEqual(shades, ["shade-0", "shade-1", "shade-2", "shade-3", "shade-0", "shade-1"])
            colours = {markers.nth(index).evaluate("(node) => getComputedStyle(node).backgroundColor") for index in range(4)}
            self.assertEqual(len(colours), 4, colours)
            page.wait_for_timeout(500)
            markers.nth(3).click()
            expect(page.locator(".movie-time")).to_have_text("00:00:06:00 / 00:00:12:00")
            page.get_by_role("button", name="Move forward one second").click()
            expect(page.locator(".movie-time")).to_have_text("00:00:07:00 / 00:00:12:00")
            page.get_by_role("button", name="Move back one second").click()
            page.get_by_role("button", name="Move back one second").click()
            expect(page.locator(".movie-time")).to_have_text("00:00:05:00 / 00:00:12:00")
            page.get_by_role("button", name="Move forward one frame").click()
            expect(page.locator(".movie-time")).to_have_text("00:00:05:01 / 00:00:12:00")
            page.get_by_role("button", name="Move back one frame").click()
            page.get_by_role("button", name="Move back one frame").click()
            expect(page.locator(".movie-time")).to_have_text("00:00:04:23 / 00:00:12:00")
            # The video sits inside frame 4:23 at 24 fps, so it shows that frame.
            self.assertAlmostEqual(page.locator("#preview video").evaluate("(video) => video.currentTime"), (4 * 24 + 23.5) / 24, places=3)
            transport = page.locator(".movie-transport button").all_inner_texts()
            self.assertEqual(transport, ["−5s", "−1s", "−1f", "Play", "+1f", "+1s", "+5s", "Note"])
            if EVIDENCE_DIR:
                EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
                (EVIDENCE_DIR / "movie-shades.png").write_bytes(page.screenshot(animations="disabled"))
            browser.close()

    def test_cli_notes_prints_notes_and_saves_screenshots(self):
        movie = self.approved_video("CLI notes clip", seconds=3)
        data_url, raw = self.jpeg_data_url()
        self.assertEqual(self.request("POST", f"/api/items/{movie['id']}/notes", {"at_seconds": 2.25, "text": "Too dark", "screenshot": data_url})[0], 201)
        out = self.root / "notes-out"
        out.mkdir()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--url", self.base, "notes", movie["id"], "--out", str(out)])
        self.assertEqual(code, 0)
        printed = json.loads(buffer.getvalue())
        self.assertEqual(printed["title"], "CLI notes clip")
        self.assertEqual([(note["at_seconds"], note["text"]) for note in printed["notes"]], [(2.25, "Too dark")])
        saved = Path(printed["notes"][0]["screenshot_file"])
        self.assertEqual((saved.parent, saved.read_bytes()), (out, raw))

    def test_agent_video_and_audio_go_straight_to_movies(self):
        for name, kind in (("Agent clip.mp4", "video"), ("Agent music.m4a", "audio")):
            status, item = self.submit(self.declaration(self.make_media(name), kind=kind, title=Path(name).stem, to="movies"))
            self.assertEqual(status, 201, item)
            self.assertEqual((item["state"], item["placed_by_agent"]), ("approved", True))
        status, human = self.submit(self.declaration(self.make_media("Human clip.mp4"), kind="video", title="Human clip"))
        self.assertEqual(status, 201, human)
        self.assertEqual((human["state"], human["placed_by_agent"]), ("candidate", False))
        status, approved = self.human(human["id"], "approve", None)
        self.assertEqual(status, 200, approved)
        self.assertFalse(approved["placed_by_agent"])
        with closing(sqlite3.connect(self.config.db_path)) as connection:
            events = connection.execute(
                """SELECT items.title, audit_events.event, audit_events.actor FROM audit_events
                   JOIN items ON items.id = audit_events.item_id
                   WHERE audit_events.event IN ('approved', 'placed_in_movies') ORDER BY items.title"""
            ).fetchall()
        self.assertEqual(events, [
            ("Agent clip", "placed_in_movies", "agent"),
            ("Agent music", "placed_in_movies", "agent"),
            ("Human clip", "approved", "human"),
        ])
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            cards = page.locator("#item-list .item-card")
            expect(cards).to_have_count(1)
            expect(cards.first).to_contain_text("Human clip")
            expect(page.locator("#artifact-count")).to_have_text("1")
            expect(page.locator("#movie-count")).to_have_text("3")
            page.click("[data-tab=movies]")
            expect(cards).to_have_count(3)
            expect(cards.locator(".agent-tag")).to_have_count(2)
            expect(cards.filter(has_text="Human clip").locator(".agent-tag")).to_have_count(0)
            cards.filter(has_text="Agent clip").click()
            expect(page.locator("#detail-kind")).to_have_text("video · approved · agent")
            expect(page.locator(".movie-editor")).to_have_count(1)
            cards.filter(has_text="Agent music").click()
            expect(page.locator("#preview audio")).to_have_count(1)
            if EVIDENCE_DIR:
                EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
                (EVIDENCE_DIR / "movies-agent-tag.png").write_bytes(page.screenshot(animations="disabled"))
            agent_clip = next(item for item in self.request("GET", "/api/items")[2]["items"] if item["title"] == "Agent clip")
            page.goto(f"{self.base}/?item={agent_clip['id']}", wait_until="networkidle", timeout=15000)
            expect(page.locator("[data-tab=movies]")).to_have_attribute("aria-selected", "true")
            expect(page.locator("#detail-title")).to_have_text("Agent clip")
            browser.close()

        status, hidden = self.human(agent_clip["id"], "hide", None)
        self.assertEqual((status, hidden["state"], hidden["placed_by_agent"]), (200, "hidden", True))
        status, restored = self.human(agent_clip["id"], "restore", None)
        self.assertEqual((status, restored["state"], restored["placed_by_agent"]), (200, "approved", True))

    def test_to_movies_refuses_other_kinds_remote_callers_and_other_targets(self):
        clip = self.make_media("clip.mp4")
        cases = [
            (self.declaration(self.patterned_image(), to="movies"), {}, "to movies takes only video, audio or movie_project"),
            (self.declaration(clip, kind="video", to="artifacts"), {}, 'to must be "movies"'),
            (self.declaration(clip, kind="video", to="movies"), {"X-Forwarded-For": "100.64.0.9"}, "to movies works only from this computer"),
        ]
        for declaration, headers, message in cases:
            status, _, body = self.request("POST", "/api/submit", declaration, headers)
            self.assertEqual(status, 400, body)
            self.assertIn(message, body["error"])
        _, _, listed = self.request("GET", "/api/items")
        self.assertEqual(listed["items"], [])

    def test_submit_only_from_this_computer_and_only_as_json(self):
        secret = self.root / "pretend_id_ed25519"
        secret.write_text("PRETEND-KEY\n")
        stolen = {"project_run_id": "x", "title": "x", "kind": "file", "source": str(secret), "expected_result": "x"}
        # DNS rebinding arrives from loopback under the attacker's Host; a proxy adds forwarding headers.
        for headers in ({"Host": "attacker.example"}, {"Host": "attacker.example:8087"}, {"X-Forwarded-For": "100.64.0.9"}):
            status, _, body = self.request("POST", "/api/submit", stolen, headers)
            self.assertEqual((status, body.get("error")), (400, "submit works only from this computer"), headers)
        _, _, listed = self.request("GET", "/api/items")
        self.assertEqual(listed["items"], [])

        status, item = self.submit(self.declaration(self.patterned_image()))
        self.assertEqual(status, 201, item)
        cross_site = ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "")
        for content_type in cross_site:
            for path, payload in (("/api/human/action", {"item_id": item["id"], "action": "delete"}), ("/api/submit", stolen)):
                headers = {"Content-Type": content_type, "Origin": "https://attacker.example"}
                status, _, body = self.request("POST", path, payload, headers)
                self.assertEqual((status, body.get("error")), (400, "request body must be sent as application/json"), (path, content_type))
        status, _, still = self.request("GET", f"/api/items/{item['id']}")
        self.assertEqual((status, still["state"]), (200, "candidate"))
        _, _, listed = self.request("GET", "/api/items")
        self.assertEqual([listed_item["id"] for listed_item in listed["items"]], [item["id"]])
        status, _, body = self.request("POST", "/api/human/action", {"item_id": item["id"], "action": "approve"}, {"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual((status, body.get("state")), (200, "approved"), body)

    def test_approve_takes_over_agent_items_and_delete_needs_no_hide(self):
        tailnet = {"X-Forwarded-For": "100.64.0.9"}
        status, agent = self.submit(self.declaration(self.make_media("Agent cut.mp4"), kind="video", title="Agent cut", to="movies"))
        self.assertEqual((status, agent["placed_by_agent"]), (201, True), agent)
        status, _, body = self.request("POST", "/api/human/action", {"item_id": agent["id"], "action": "approve"}, tailnet)
        self.assertEqual((status, body.get("pin_required")), (403, True), body)
        status, approved = self.human(agent["id"], "approve", None)
        self.assertEqual((status, approved["state"], approved["placed_by_agent"]), (200, "approved", False), approved)
        self.assertEqual([event["event"] for event in self.audit_events(agent["id"])][-2:], ["placed_in_movies", "approved"])
        status, again = self.human(agent["id"], "approve", None)
        self.assertEqual((status, again["error"]), (409, "only verified candidates can be approved"))

        human = self.approved_video("Human cut", seconds=2)
        folder = self.config.approved_dir / human["id"]
        self.assertTrue(folder.exists())
        status, _, body = self.request("POST", "/api/human/action", {"item_id": human["id"], "action": "delete"}, tailnet)
        self.assertEqual((status, body.get("pin_required")), (403, True), body)
        status, deleted = self.human(human["id"], "delete", None)
        self.assertEqual((status, deleted["state"]), (200, "deleted"), deleted)
        self.assertFalse(folder.exists())
        status, twice = self.human(human["id"], "delete", None)
        self.assertEqual((status, twice["error"]), (409, "item is already deleted"))

    def test_movies_list_sorts_selects_approves_and_deletes(self):
        for title in ("Bravo", "Alpha", "Charlie"):
            self.approved_video(title, seconds=2)
        status, agent = self.submit(self.declaration(self.make_media("Delta.mp4"), kind="video", title="Delta", to="movies"))
        self.assertEqual(status, 201, agent)
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            page.click("[data-tab=movies]")
            cards = page.locator("#item-list .item-card")
            titles = page.locator("#item-list .item-card strong")
            expect(cards).to_have_count(4)
            expect(page.locator("#sort-control")).to_be_visible()
            expect(page.locator("#item-list input[type=checkbox]")).to_have_count(4)
            page.select_option("#sort", "name-az")
            expect(titles).to_have_text(["Alpha", "Bravo", "Charlie", "Delta"])
            page.fill("#filter", "ar")
            expect(titles).to_have_text(["Charlie"])
            page.fill("#filter", "")

            actions = page.locator("#detail-actions button")
            cards.filter(has_text="Delta").click()
            expect(actions).to_have_text(["Open in folder", "Approve", "Hide", "Delete"])
            if EVIDENCE_DIR:
                EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
                (EVIDENCE_DIR / "movies-list-actions.png").write_bytes(page.screenshot(animations="disabled"))
            page.locator("#detail-actions").get_by_role("button", name="Approve", exact=True).click()
            expect(cards.filter(has_text="Delta").locator(".agent-tag")).to_have_count(0)
            expect(actions).to_have_text(["Open in folder", "Hide", "Delete"])

            cards.filter(has_text="Alpha").click()
            expect(actions).to_have_text(["Open in folder", "Hide", "Delete"])
            page.locator("#detail-actions").get_by_role("button", name="Delete", exact=True).click()
            expect(cards).to_have_count(3)
            expect(page.locator("#detail")).to_be_hidden()

            page.get_by_label("Select Bravo").check()
            page.get_by_label("Select Charlie").check()
            expect(page.locator("#delete-selected")).to_have_text("Delete 2 selected")
            page.once("dialog", lambda dialog: dialog.accept())
            page.click("#delete-selected")
            expect(titles).to_have_text(["Delta"])

            page.click("[data-tab=artifacts]")
            expect(page.locator("#sort-control")).to_be_hidden()
            expect(page.locator("#item-list input[type=checkbox]")).to_have_count(0)
            browser.close()
        states = {item["title"]: item["state"] for item in self.request("GET", "/api/items")[2]["items"]}
        self.assertEqual(states, {"Delta": "approved"})

    def test_failed_verification_with_to_movies_stays_in_review(self):
        broken = self.root / "broken.mp4"
        broken.write_bytes(os.urandom(4096))
        status, item = self.submit(self.declaration(broken, kind="video", to="movies"))
        self.assertEqual(status, 201, item)
        self.assertEqual((item["state"], item["placed_by_agent"]), ("candidate", False))
        self.assertNotEqual(item["verification_status"], "ready")

    def test_cli_submit_to_movies(self):
        clip = self.make_media("cli clip.mp4")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([
                "--url", self.base, "submit", str(clip), "--project-run-id", "project/e2e", "--title", "CLI clip",
                "--kind", "video", "--expected-result", "plays", "--to", "movies",
            ])
        self.assertEqual(code, 0)
        submitted = json.loads(buffer.getvalue())
        self.assertEqual((submitted["state"], submitted["placed_by_agent"]), ("approved", True))

    def test_database_upgrade_adds_placed_by_agent(self):
        database = Database(self.config.db_path)
        columns = lambda: {row["name"] for row in connection.execute("PRAGMA table_info(items)")}
        with database.connect() as connection:
            connection.execute("ALTER TABLE items DROP COLUMN placed_by_agent")
            self.assertNotIn("placed_by_agent", columns())
        database.initialize()
        with database.connect() as connection:
            self.assertIn("placed_by_agent", columns())
        status, item = self.submit(self.declaration(self.patterned_image()))
        self.assertEqual((status, item["placed_by_agent"]), (201, False))

    def test_review_inbox_sorts_by_date_size_and_name(self):
        for title, side in (("Banana", 24), ("apple", 96), ("Cherry", 48)):
            path = self.root / f"{title}.png"
            Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(path)
            status, item = self.submit(self.declaration(path, title=title))
            self.assertEqual(status, 201, item)
        _, _, listed = self.request("GET", "/api/items")
        sizes = {item["title"]: item["size_bytes"] for item in listed["items"]}
        self.assertLess(sizes["Banana"], sizes["Cherry"])
        self.assertLess(sizes["Cherry"], sizes["apple"])

        expected = {
            "newest": ["Cherry", "apple", "Banana"],
            "oldest": ["Banana", "apple", "Cherry"],
            "largest": ["apple", "Cherry", "Banana"],
            "smallest": ["Banana", "Cherry", "apple"],
            "name-az": ["apple", "Banana", "Cherry"],
            "name-za": ["Cherry", "Banana", "apple"],
        }
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            self.assertFalse(page.locator("#sort-control").is_visible())
            page.click("[data-tab=review]")
            self.assertTrue(page.locator("#sort-control").is_visible())
            for value, titles in expected.items():
                page.select_option("#sort", value)
                self.assertEqual(page.locator("#item-list .item-card strong").all_inner_texts(), titles, value)
            page.reload(wait_until="networkidle")
            page.click("[data-tab=review]")
            self.assertEqual(page.locator("#sort").input_value(), "name-za")
            self.assertEqual(page.locator("#item-list .item-card strong").all_inner_texts(), expected["name-za"])
            if EVIDENCE_DIR:
                EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
                (EVIDENCE_DIR / "review-sort.png").write_bytes(page.screenshot(animations="disabled"))
            browser.close()

    def test_review_inbox_deletes_checked_items(self):
        ids = {}
        for title, side in (("Banana", 24), ("apple", 32), ("Cherry", 40), ("Kept", 48)):
            path = self.root / f"{title}.png"
            Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(path)
            status, item = self.submit(self.declaration(path, title=title))
            self.assertEqual(status, 201, item)
            ids[title] = item["id"]
        status, body = self.human(ids["Kept"], "approve", pin=None)
        self.assertEqual(status, 200, body)

        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            self.assertEqual(page.locator("#item-list input[type=checkbox]").count(), 0)
            page.click("[data-tab=review]")
            self.assertEqual(page.locator("#item-list input[type=checkbox]").count(), 3)
            self.assertFalse(page.locator("#delete-selected").is_visible())
            page.get_by_label("Select Banana").check()
            page.get_by_label("Select Cherry").check()
            self.assertEqual(page.locator("#delete-selected").inner_text(), "Delete 2 selected")
            self.assertFalse(page.locator("#detail").is_visible())

            page.once("dialog", lambda dialog: dialog.dismiss())
            page.click("#delete-selected")
            _, _, listed = self.request("GET", "/api/items")
            self.assertEqual(len(listed["items"]), 4)

            page.once("dialog", lambda dialog: dialog.accept())
            page.click("#delete-selected")
            expect(page.locator("#item-list .item-card")).to_have_count(1)
            self.assertEqual(page.locator("#item-list .item-card strong").all_inner_texts(), ["apple"])
            self.assertFalse(page.locator("#delete-selected").is_visible())
            if EVIDENCE_DIR:
                EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
                (EVIDENCE_DIR / "review-multi-delete.png").write_bytes(page.screenshot(animations="disabled"))
            browser.close()
        _, _, listed = self.request("GET", "/api/items")
        self.assertEqual(sorted(item["title"] for item in listed["items"]), ["Kept", "apple"])

    def test_replaces_deletes_the_older_candidate_once_the_new_one_is_ready(self):
        status, old = self.submit(self.declaration(self.patterned_image("v1.png"), title="v1"))
        self.assertEqual(status, 201, old)
        status, new = self.submit(self.declaration(self.patterned_image("v2.png"), title="v2", replaces=old["id"]))
        self.assertEqual(status, 201, new)
        self.assertEqual(new["verification_status"], "ready")
        self.assertEqual(new["replaced"], old["id"])
        _, _, gone = self.request("GET", f"/api/items/{old['id']}")
        self.assertEqual(gone["state"], "deleted")
        events = [event["event"] for event in self.server.store.audit(old["id"])]
        self.assertEqual(events[-2:], ["replaced", "deleted"])

    def test_replaces_keeps_the_older_candidate_when_the_new_one_is_rejected(self):
        status, old = self.submit(self.declaration(self.patterned_image("v1.png"), title="v1"))
        self.assertEqual(status, 201, old)
        blank = self.root / "blank.png"
        Image.new("RGB", (80, 80), "white").save(blank)
        status, new = self.submit(self.declaration(blank, title="v2", replaces=old["id"]))
        self.assertEqual(status, 201, new)
        self.assertEqual(new["verification_status"], "rejected")
        self.assertIsNone(new["replaced"])
        _, _, kept = self.request("GET", f"/api/items/{old['id']}")
        self.assertEqual(kept["state"], "candidate")

    def test_replaces_refuses_approved_items_other_runs_and_bad_ids_before_staging(self):
        status, approved = self.submit(self.declaration(self.patterned_image("a.png"), title="approved"))
        self.assertEqual(self.human(approved["id"], "approve", pin=None)[0], 200)
        status, other = self.submit(self.declaration(self.patterned_image("o.png"), title="other run",
                                                     project_run_id="project/other"))
        self.assertEqual(status, 201, other)
        for replaces, code in ((approved["id"], 409), (other["id"], 409), ("not-an-id", 400), ("0" * 32, 404)):
            status, body = self.request("POST", "/api/submit", self.declaration(
                self.patterned_image("v2.png"), title="v2", replaces=replaces))[0::2]
            self.assertEqual(status, code, body)
        _, _, listed = self.request("GET", "/api/items")
        self.assertEqual(sorted(item["title"] for item in listed["items"]), ["approved", "other run"])
        _, _, kept = self.request("GET", f"/api/items/{approved['id']}")
        self.assertEqual(kept["state"], "approved")

    def test_cli_submit_replaces_an_older_candidate(self):
        status, old = self.submit(self.declaration(self.patterned_image("v1.png"), title="v1"))
        self.assertEqual(status, 201, old)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([
                "--url", self.base, "submit", str(self.patterned_image("v2.png")),
                "--project-run-id", "project/e2e", "--title", "v2", "--kind", "image",
                "--expected-result", "A visible valid sample", "--replaces", old["id"],
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["replaced"], old["id"])
        _, _, gone = self.request("GET", f"/api/items/{old['id']}")
        self.assertEqual(gone["state"], "deleted")

    def test_cli_submit_and_open_the_same_item(self):
        image = self.patterned_image("shared.png")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([
                "--url", self.base, "submit", str(image),
                "--project-run-id", "project/e2e", "--title", "shared.png",
                "--kind", "image", "--expected-result", "A visible valid sample",
            ])
        self.assertEqual(code, 0)
        submitted = json.loads(buffer.getvalue())
        item_id = submitted["id"]
        self.assertEqual(submitted["state"], "candidate")
        expected_url = previewer_item_url(self.base, item_id)
        self.assertEqual(expected_url, f"{self.base}/?item={item_id}")

        open_buffer = io.StringIO()
        with redirect_stdout(open_buffer):
            code = main(["--url", self.base, "open", item_id, "--no-browser"])
        self.assertEqual(code, 0)
        cli_url = open_buffer.getvalue().strip()
        self.assertEqual(cli_url, expected_url)

        desktop, mobile = _capture_item(self.base, item_id, self.config.chromium_path)
        if EVIDENCE_DIR:
            EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
            (EVIDENCE_DIR / "desktop-same-item.png").write_bytes(desktop)
            (EVIDENCE_DIR / "mobile-same-item.png").write_bytes(mobile)
            (EVIDENCE_DIR / "report.json").write_text(json.dumps({
                "item_id": item_id,
                "cli_url": cli_url,
                "same_item": cli_url == expected_url,
            }, indent=2), encoding="utf-8")
        self.assertTrue(_screenshot_is_visible(desktop))
        self.assertTrue(_screenshot_is_visible(mobile))

    def model_item(self, name: str, data: bytes, title: str) -> dict:
        source = self.root / name
        source.write_bytes(data)
        status, item = self.submit(self.declaration(source, kind="model3d", title=title))
        self.assertEqual((status, item["verification_status"]), (201, "ready"), item)
        return item

    def model_rows(self, page, selector: str) -> dict:
        return page.locator(f"{selector} dl").evaluate(
            "(list) => Object.fromEntries([...list.querySelectorAll('dt')].map((term) => [term.textContent, term.nextElementSibling.textContent]))"
        )

    def canvas_colour(self, page, selector: str = ".model-canvas") -> tuple[float, float, float]:
        shot = Image.open(io.BytesIO(page.locator(selector).screenshot())).convert("RGB")
        width, height = shot.size
        return tuple(ImageStat.Stat(shot.crop((width // 2 - 20, height // 2 - 20, width // 2 + 20, height // 2 + 20))).mean)

    def test_model_viewer_shows_checks_animation_notes_and_slicer(self):
        model = self.model_item("cube.glb", cube_glb(texture=checker_png(), animate=True), "Spinning cube")
        opened = []
        self.server.open_in_slicer = opened.append
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            problems = []
            page.on("console", lambda message: problems.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            # 3D models have their own tab and stay out of Review Inbox.
            expect(page.locator("#model-count")).to_have_text("1")
            expect(page.locator("#review-count")).to_have_text("0")
            page.click("[data-tab=review]")
            expect(page.locator("#item-list .item-card", has_text="Spinning cube")).to_have_count(0)
            page.click("[data-tab=models]")
            expect(page.locator("#section-title")).to_have_text("3D Models")
            expect(page.locator("#sort-control")).to_be_visible()
            expect(page.locator("#item-list input[type=checkbox]")).to_have_count(1)
            page.locator("#item-list .item-card", has_text="Spinning cube").click()
            expect(page.locator(".model-stats")).to_have_text("12 triangles · 50.0 × 50.0 × 50.0 mm", timeout=20000)
            self.assertEqual(page.locator("#detail-actions button").all_inner_texts(), ["Open in folder", "Open in slicer", "Approve", "Delete candidate"])
            expect(page.get_by_text("Drag to rotate · wheel to zoom", exact=True)).to_have_count(0)
            files = self.model_rows(page, ".model-file-check")
            self.assertEqual({key: files[key] for key in ("Format", "Triangles", "Vertices", "Meshes", "Materials", "Animations")}, {
                "Format": "GLB (glTF 2.0)", "Triangles": "12", "Vertices": "24", "Meshes": "1", "Materials": "1", "Animations": "spin (2.00 s)",
            })
            expect(page.locator(".model-map figcaption")).to_have_text(["Colour · 8 × 8"])
            expect(page.locator(".model-print-list")).to_have_count(1, timeout=20000)
            # Exporters split vertices at seams; the check welds them, so this cube is closed.
            self.assertEqual(self.model_rows(page, ".model-print-check"), {
                "Size": "50.0 × 50.0 × 50.0 mm (width × depth × height)", "Watertight": "Yes", "Broken edges": "None",
                "Separate pieces": "1", "Volume": "125 cm³", "Grid squares": "10 mm",
            })
            expect(page.locator(".model-unit-note")).to_have_text("glTF sizes are in metres; shown here in millimetres.")

            play = page.get_by_role("button", name="Play or pause the animation")
            expect(play).to_have_text("Pause")
            expect(page.get_by_role("combobox", name="Animation")).to_have_value("0")
            play.click()
            expect(play).to_have_text("Play")
            page.locator(".model-animation input[type=range]").evaluate("(input) => { input.value = '1'; input.dispatchEvent(new Event('input')); }")
            expect(page.locator(".model-time")).to_have_text("1.00 s / 2.00 s")
            page.wait_for_timeout(300)
            textured = self.canvas_colour(page)
            page.get_by_role("button", name="Show the model in plain clay").click()
            expect(page.get_by_role("button", name="Show the model in plain clay")).to_have_attribute("aria-pressed", "true")
            expect(page.get_by_role("button", name="Show the model with its textures")).to_have_attribute("aria-pressed", "false")
            page.wait_for_timeout(300)
            clay = self.canvas_colour(page)
            self.assertGreater(max(textured) - min(textured), 40, textured)
            self.assertLess(max(clay) - min(clay), 25, clay)
            self.assertGreater(sum(clay) / 3, 70, clay)
            page.get_by_role("button", name="Spin the model").click()
            expect(page.get_by_role("button", name="Spin the model")).to_have_attribute("aria-pressed", "true")
            page.get_by_role("button", name="Spin the model").click()
            page.get_by_role("button", name="Show the floor grid").click()
            expect(page.get_by_role("button", name="Show the floor grid")).to_have_attribute("aria-pressed", "false")

            page.keyboard.press("n")
            canvas = page.locator(".note-canvas")
            expect(canvas).to_be_visible()
            expect(page.locator(".note-stamp")).to_have_text("View 1")
            box = canvas.bounding_box()
            page.mouse.move(box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.3)
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.6, steps=6)
            page.mouse.up()
            page.get_by_role("textbox", name="Note").fill("Top face is too shiny")
            page.keyboard.press("Enter")
            expect(page.locator(".note-layer")).to_have_count(0)
            expect(page.locator(".note-row .note-time")).to_have_text(["View 1"])
            expect(page.locator(".note-row .note-body")).to_have_text(["Top face is too shiny"])
            notes = self.request("GET", f"/api/items/{model['id']}/notes")[2]["notes"]
            self.assertEqual(len(notes), 1)
            note = notes[0]
            self.assertEqual((note["text"], note["at_seconds"], note["view"]["mode"], note["view"]["clip"]), ("Top face is too shiny", 1.0, "clay", "spin"))
            self.assertTrue(all(len(note["view"][key]) == 3 for key in ("position", "target")), note["view"])
            with urlopen(self.base + note["screenshot_url"]) as response:
                shot = Image.open(io.BytesIO(response.read())).convert("RGB")
            # Clay has no red, so red pixels in the saved frame are the box the test drew.
            pixels = shot.load()
            red = sum(1 for y in range(shot.height) for x in range(shot.width) if pixels[x, y][0] > 160 and pixels[x, y][1] < 90 and pixels[x, y][2] < 90)
            self.assertGreater(red, 100, red)
            if EVIDENCE_DIR:
                EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
                (EVIDENCE_DIR / "model-viewer.png").write_bytes(page.screenshot(animations="disabled"))

            page.get_by_role("button", name="Show the model as a wireframe").click()
            canvas_box = page.locator(".model-canvas").bounding_box()
            page.mouse.move(canvas_box["x"] + canvas_box["width"] / 2, canvas_box["y"] + canvas_box["height"] / 2)
            page.mouse.down()
            page.mouse.move(canvas_box["x"] + canvas_box["width"] / 2 + 200, canvas_box["y"] + canvas_box["height"] / 2, steps=8)
            page.mouse.up()
            page.locator(".model-animation input[type=range]").evaluate("(input) => { input.value = '0'; input.dispatchEvent(new Event('input')); }")
            page.get_by_role("button", name="Jump to note for view 1").click()
            expect(page.get_by_role("button", name="Show the model in plain clay")).to_have_attribute("aria-pressed", "true")
            expect(page.locator(".model-time")).to_have_text("1.00 s / 2.00 s")
            page.get_by_role("button", name="Edit note for view 1").click()
            page.get_by_role("textbox", name="Edit note").fill("Top face is far too shiny")
            page.keyboard.press("Enter")
            expect(page.locator(".note-row .note-body")).to_have_text(["Top face is far too shiny"])

            page.get_by_role("button", name="Open in slicer").click()
            expect(page.get_by_role("button", name="Opened")).to_have_count(1)
            self.assertEqual(opened, [self.config.data_dir / "slicer" / f"{model['id']}.stl"])
            data = opened[0].read_bytes()
            self.assertEqual(len(data), 84 + 50 * 12)
            corners = [value for index in range(12) for value in struct.unpack_from("<9f", data, 84 + 50 * index + 12)]
            self.assertAlmostEqual(max(abs(value) for value in corners), 25.0, places=3)

            self.assertEqual(self.human(model["id"], "approve", None)[0], 200)
            page.goto(f"{self.base}/?item={model['id']}", wait_until="networkidle")
            # A link to a 3D model opens the 3D Models tab; approved, it stays there and not in Live.
            expect(page.locator("[data-tab=models]")).to_have_attribute("aria-selected", "true")
            expect(page.locator("#live-count")).to_have_text("0")
            expect(page.locator("#preview .model-stats")).to_have_text("12 triangles · 50.0 × 50.0 × 50.0 mm", timeout=20000)
            self.assertEqual(page.locator("#detail-actions button").all_inner_texts(), ["Open in folder", "Open in slicer", "Hide", "Delete"])
            expect(page.locator("#preview .note-row")).to_have_count(1)
            page.get_by_role("button", name="Delete note for view 1").click()
            expect(page.locator("#preview .note-row")).to_have_count(0)
            self.assertEqual(self.request("GET", f"/api/items/{model['id']}/notes")[2]["notes"], [])
            page.click("[data-tab=live]")
            expect(page.locator("#item-list .item-card", has_text="Spinning cube")).to_have_count(0)
            page.click("[data-tab=models]")
            page.locator("#item-list .item-card", has_text="Spinning cube").click()
            page.get_by_role("button", name="Delete", exact=True).click()
            expect(page.locator("#model-count")).to_have_text("0")
            self.assertEqual(self.request("GET", f"/api/items/{model['id']}")[2]["state"], "deleted")
            self.assertEqual(problems, [])
            browser.close()

    def test_stl_and_3mf_models_and_unreadable_files_in_the_viewer(self):
        part = self.model_item("part.stl", binary_stl(cube_triangles(20)), "Printed part")
        self.model_item("open.stl", binary_stl(cube_triangles(20, drop_faces=1)), "Open box")
        self.model_item("pair.stl", binary_stl(cube_triangles(10) + cube_triangles(10, offset=(30, 0, 0))), "Two blocks")
        self.model_item("cube.3mf", three_mf(cube_triangles(1), unit="centimeter"), "Centimetre cube")
        self.model_item("packed.glb", draco_glb(), "Packed model")
        opened = []
        self.server.open_in_slicer = opened.append
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            page.click("[data-tab=models]")

            def open_model(title: str) -> dict:
                page.locator("#item-list .item-card", has_text=title).click()
                expect(page.locator(".model-print-list")).to_have_count(1, timeout=20000)
                return self.model_rows(page, ".model-print-check")

            rows = open_model("Printed part")
            expect(page.locator(".model-stats")).to_have_text("12 triangles · 20.0 × 20.0 × 20.0 mm")
            self.assertEqual((rows["Watertight"], rows["Volume"], rows["Separate pieces"]), ("Yes", "8.0 cm³", "1"))
            self.assertEqual(self.model_rows(page, ".model-file-check")["Format"], "STL (binary)")
            expect(page.locator(".model-unit-note")).to_have_text("STL files name no unit; read as millimetres, as slicers do.")
            self.assertGreater(sum(self.canvas_colour(page)) / 3, 70)
            # The file's facet normals are zero, as many exporters write them; the faces must still shade apart.
            faces = page.locator(".model-canvas").screenshot()
            with Image.open(io.BytesIO(faces)) as shot:
                width, height = shot.size
                left = ImageStat.Stat(shot.convert("L").crop((width * 40 // 100 - 10, height * 3 // 5 - 10, width * 40 // 100 + 10, height * 3 // 5 + 10))).mean[0]
                right = ImageStat.Stat(shot.convert("L").crop((width * 72 // 100 - 10, height * 3 // 5 - 10, width * 72 // 100 + 10, height * 3 // 5 + 10))).mean[0]
            self.assertGreater(abs(left - right), 8, (left, right))
            page.get_by_role("button", name="Open in slicer").click()
            expect(page.get_by_role("button", name="Opened")).to_have_count(1)
            self.assertEqual(opened, [self.root / "part.stl"])
            if EVIDENCE_DIR:
                EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
                (EVIDENCE_DIR / "model-viewer-stl.png").write_bytes(page.screenshot(animations="disabled"))

            rows = open_model("Open box")
            self.assertEqual((rows["Watertight"], rows["Broken edges"], rows["Volume"]), ("No: 4 open edges", "None", "Needs a watertight mesh"))
            expect(page.locator(".model-print-list dd.check-bad")).to_have_count(1)
            self.assertEqual(open_model("Two blocks")["Separate pieces"], "2")
            rows = open_model("Centimetre cube")
            expect(page.locator(".model-stats")).to_have_text("12 triangles · 10.0 × 10.0 × 10.0 mm")
            self.assertEqual((rows["Volume"], self.model_rows(page, ".model-file-check")["Format"]), ("1.0 cm³", "3MF"))
            expect(page.locator(".model-unit-note")).to_have_text("3MF unit converted to millimetres (×10).")

            page.locator("#item-list .item-card", has_text="Packed model").click()
            expect(page.locator(".model-status")).to_have_text("This GLB is Draco-compressed. Previewer's viewer does not open Draco files.", timeout=20000)
            expect(page.get_by_role("button", name="Add a note at this view")).to_be_disabled()

            # The TV's 960 px: the panel goes under the viewer and nothing scrolls sideways.
            page.set_viewport_size({"width": 960, "height": 900})
            open_model("Printed part")
            stage = page.locator(".model-stage").bounding_box()
            panel = page.locator(".model-inspector").bounding_box()
            self.assertGreaterEqual(panel["y"], stage["y"] + stage["height"] - 1)
            self.assertGreater(stage["height"], 400)
            self.assertEqual(page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth"), 0)
            if EVIDENCE_DIR:
                (EVIDENCE_DIR / "model-tab-960.png").write_bytes(page.screenshot(animations="disabled"))
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator("[data-tab=models]")).to_be_visible()
            self.assertEqual(page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth"), 0)
            browser.close()

    def model_extent(self, page) -> tuple[float, float]:
        """Leftmost and rightmost columns, as fractions of the canvas width, that show the model, not background.

        Only the middle band is read: the toolbar sits over the top and the readouts over the bottom.
        """
        shot = Image.open(io.BytesIO(page.locator(".model-canvas").screenshot())).convert("RGB")
        width, height = shot.size
        pixels = shot.load()
        rows = range(height // 4, height * 3 // 4, 4)
        columns = [x for x in range(width) if any(max(abs(a - b) for a, b in zip(pixels[x, y], (21, 25, 30))) > 40 for y in rows)]
        self.assertTrue(columns, "model not drawn")
        return columns[0] / width, (columns[-1] + 1) / width

    def test_expand_fills_the_width_and_3d_zoom(self):
        status, image = self.submit(self.declaration(self.patterned_image(), title="Wide pattern"))
        self.assertEqual(self.human(image["id"], "approve", None)[0], 200)
        self.model_item("cube.glb", cube_glb(), "Zoom cube")
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.config.chromium_path)
            page = browser.new_page(viewport={"width": 2400, "height": 1100})
            page.goto(self.base, wait_until="networkidle", timeout=15000)
            stage = page.locator(".stage")
            shell = page.locator("#preview-shell")
            # Expand used to grow only the height; now it also takes the width beside the list.
            for tab, title in (("artifacts", "Wide pattern"), ("models", "Zoom cube")):
                page.click(f"[data-tab={tab}]")
                page.locator("#item-list .item-card", has_text=title).click()
                before = shell.bounding_box()["width"]
                self.assertLessEqual(before, 1500, tab)
                page.locator("#preview-shell > .expand-toggle").click()
                expect(page.locator("#preview-shell > .expand-toggle")).to_have_text("Minimise")
                after = shell.bounding_box()["width"]
                self.assertGreater(after, before + 400, tab)
                self.assertAlmostEqual(after, stage.bounding_box()["width"] - 48, delta=2, msg=tab)
                page.locator("#preview-shell > .expand-toggle").click()
                expect(page.locator("#detail")).not_to_have_class("detail wide")
                self.assertAlmostEqual(shell.bounding_box()["width"], before, delta=2, msg=tab)

            expect(page.locator(".model-stats")).to_have_text("12 triangles · 50.0 × 50.0 × 50.0 mm", timeout=20000)
            page.get_by_role("button", name="Show the floor grid").click()
            page.wait_for_timeout(300)
            left, right = self.model_extent(page)
            page.get_by_role("button", name="Zoom in").click()
            page.wait_for_timeout(300)
            zoomed = self.model_extent(page)
            self.assertGreater(zoomed[1] - zoomed[0], (right - left) * 1.15, (left, right, zoomed))
            page.get_by_role("button", name="Zoom out").click()
            page.get_by_role("button", name="Zoom out").click()
            page.wait_for_timeout(300)
            smaller = self.model_extent(page)
            self.assertLess(smaller[1] - smaller[0], right - left, (left, right, smaller))
            page.get_by_role("button", name="Reset the camera").click()
            page.wait_for_timeout(300)
            reset = self.model_extent(page)
            self.assertAlmostEqual(reset[1] - reset[0], right - left, delta=0.02)

            # A tall, narrow viewer (expanded on a portrait window) frames the model again instead of cropping it.
            page.set_viewport_size({"width": 1300, "height": 1700})
            page.locator("#preview-shell > .expand-toggle").click()
            page.wait_for_timeout(500)
            canvas = page.locator(".model-canvas").bounding_box()
            self.assertLess(canvas["width"] / canvas["height"], 0.5, canvas)
            tall = self.model_extent(page)
            self.assertGreater(tall[0], 0.02, tall)
            self.assertLess(tall[1], 0.98, tall)
            if EVIDENCE_DIR:
                EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
                page.set_viewport_size({"width": 2400, "height": 1100})
                page.wait_for_timeout(500)
                (EVIDENCE_DIR / "expand-wide-3d.png").write_bytes(page.screenshot(animations="disabled"))
            browser.close()

    def test_model_note_and_slicer_routes(self):
        model = self.model_item("cube.glb", cube_glb(), "Route cube")
        part = self.model_item("part.stl", binary_stl(cube_triangles(20)), "Route part")
        opened = []
        self.server.open_in_slicer = opened.append
        notes_path = f"/api/items/{model['id']}/notes"
        view = {"position": [0.1, 0.06, 0.15], "target": [0, 0, 0], "mode": "textured"}
        status, _, note = self.request("POST", notes_path, {"at_seconds": 0, "text": "Base too thin", "view": view})
        self.assertEqual((status, note["view"]), (201, view), note)
        self.assertEqual(self.request("GET", notes_path)[2]["notes"][0]["view"], view)
        status, _, body = self.request("POST", notes_path, {"at_seconds": 0, "text": "No view"})
        self.assertEqual((status, body["error"]), (400, "a note on a 3D model needs its view"))

        self.assertEqual(self.request("POST", f"/api/items/{part['id']}/open-in-slicer", {})[2], {"opened": "original"})
        self.assertEqual(opened, [self.root / "part.stl"])
        status, _, body = self.request("POST", f"/api/items/{model['id']}/open-in-slicer", {})
        self.assertEqual(status, 409, body)

        def send_stl(item_id: str, data: bytes) -> tuple[int, dict]:
            request = Request(f"{self.base}/api/items/{item_id}/open-in-slicer", data=data, method="POST", headers={"Content-Type": "model/stl"})
            try:
                with urlopen(request, timeout=30) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as exc:
                with exc:
                    return exc.code, json.loads(exc.read())

        self.assertEqual(send_stl(model["id"], binary_stl(cube_triangles(50))), (200, {"opened": "stl copy"}))
        self.assertEqual(opened[-1], self.config.data_dir / "slicer" / f"{model['id']}.stl")
        self.assertEqual(send_stl(model["id"], b"x" * 100)[0], 400)
        status, image = self.submit(self.declaration(self.patterned_image()))
        self.assertEqual(send_stl(image["id"], binary_stl(cube_triangles(5)))[0], 409)
        self.assertEqual(len(opened), 2)


def _capture_item(base: str, item_id: str, chromium: str) -> tuple[bytes, bytes]:
    from playwright.sync_api import sync_playwright

    url = f"{base}/?item={item_id}"
    shots = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=chromium)
        for width, height in ((1440, 900), (390, 844)):
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(url, wait_until="networkidle", timeout=15000)
            page.wait_for_selector("#detail:not([hidden])", timeout=15000)
            title = page.locator("#detail-title").inner_text()
            if "shared.png" not in title:
                raise AssertionError(f"opened item title was {title!r}")
            shots.append(page.screenshot(full_page=True, animations="disabled"))
            page.close()
        browser.close()
    return shots[0], shots[1]


def _screenshot_is_visible(payload: bytes) -> bool:
    with Image.open(io.BytesIO(payload)) as image:
        sample = image.convert("RGB")
        sample.thumbnail((160, 160))
        stat = ImageStat.Stat(sample)
        return sum(stat.var) >= 1.0 and statistics.fmean(stat.mean) > 1.0


if __name__ == "__main__":
    unittest.main()
