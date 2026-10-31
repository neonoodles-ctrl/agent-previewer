from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from previewer.config import Config
from previewer.security import AuthError, PinGuard, RateLimited, hash_pin, verify_pin
from previewer.service import _public_item
from previewer.store import CapacityError, Conflict, NotFound, Store, StoreError

from model_fixtures import binary_stl, cube_glb, cube_triangles, three_mf


class TargetHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"""<!doctype html><style>body{margin:0;background:#edf2ef;color:#122}main{min-height:2400px;padding:30px;background:linear-gradient(#edf2ef,#b8d6cf)}canvas{width:320px;height:180px}button{padding:12px}</style><main><h1 id='title'>Target ready</h1><button id='change' onclick=\"document.getElementById('status').textContent='changed'\">Change</button><p id='status'>waiting</p><canvas id='screen' width='320' height='180'></canvas><script>const c=document.getElementById('screen').getContext('2d');c.fillStyle='#167b6f';c.fillRect(0,0,160,180);c.fillStyle='#d7ff5f';c.fillRect(160,0,160,180)</script></main>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class PreviewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(data_dir=self.root / "data", volume_limit_bytes=20 * 1024 * 1024)
        self.store = Store(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def declaration(self, source: Path | str, kind: str = "image", **extra):
        return {
            "project_run_id": "project/run-17",
            "title": "Verification sample",
            "kind": kind,
            "source": str(source),
            "expected_result": "A visible, valid sample",
            **extra,
        }

    def patterned_image(self, name="pattern.png") -> Path:
        path = self.root / name
        image = Image.new("RGB", (64, 64))
        pixels = image.load()
        for y in range(64):
            for x in range(64):
                pixels[x, y] = (230, 70, 60) if (x // 8 + y // 8) % 2 else (30, 90, 210)
        image.save(path)
        return path

    def test_image_lifecycle_is_verified_audited_and_immutable(self):
        item = self.store.submit(self.declaration(self.patterned_image()))
        self.assertEqual(item["verification_status"], "pending")
        item = self.store.verify(item["id"])
        self.assertEqual(item["verification_status"], "ready")
        self.assertTrue(item["receipt"]["evidence"])
        item = self.store.approve(item["id"])
        self.assertEqual(item["state"], "approved")
        self.assertIsNone(item["staged_path"])
        approved = Path(item["approved_path"])
        self.assertEqual(approved.stat().st_mode & 0o777, 0o444)
        self.assertEqual(self.store.content_path(item["id"]), approved)
        self.store.hide(item["id"])
        self.store.restore(item["id"])
        events = [event["event"] for event in self.store.audit(item["id"])]
        self.assertEqual(events, ["submitted", "verification_ready", "approved", "hidden", "restored"])

    def test_corrupt_and_blank_images_fail(self):
        corrupt = self.root / "broken.png"
        corrupt.write_bytes(b"not an image")
        rejected = self.store.verify(self.store.submit(self.declaration(corrupt))["id"])
        self.assertEqual(rejected["verification_status"], "rejected")
        blank = self.root / "blank.png"
        Image.new("RGB", (80, 80), "white").save(blank)
        rejected = self.store.verify(self.store.submit(self.declaration(blank))["id"])
        self.assertEqual(rejected["verification_status"], "rejected")

    def test_candidate_cannot_be_approved_before_verification(self):
        item = self.store.submit(self.declaration(self.patterned_image()))
        with self.assertRaises(Conflict):
            self.store.approve(item["id"])

    def test_candidate_content_path_returns_staged_file(self):
        source = self.patterned_image()
        item = self.store.verify(self.store.submit(self.declaration(source))["id"])
        self.assertEqual(item["state"], "candidate")
        self.assertEqual(self.store.content_path(item["id"]), Path(item["staged_path"]))

    def test_folder_target_prefers_original_then_copy(self):
        source = self.patterned_image()
        item = self.store.verify(self.store.submit(self.declaration(source))["id"])
        self.assertEqual(self.store.folder_target(item["id"]), (source, "original"))
        source.unlink()
        self.assertEqual(self.store.folder_target(item["id"]), (Path(item["staged_path"]), "copy"))
        Path(item["staged_path"]).unlink()
        with self.assertRaises(NotFound):
            self.store.folder_target(item["id"])

    def test_approved_item_deletes_without_hiding(self):
        item = self.store.submit(self.declaration(self.patterned_image()))
        item = self.store.approve(self.store.verify(item["id"])["id"])
        approved = Path(item["approved_path"])
        deleted = self.store.delete(item["id"])
        self.assertEqual(deleted["state"], "deleted")
        self.assertFalse(approved.exists())
        self.assertEqual([event["event"] for event in self.store.audit(item["id"])][-2:], ["approved", "deleted"])
        with self.assertRaises(Conflict):
            self.store.delete(item["id"])

    def test_approved_tampering_is_detected_on_read(self):
        item = self.store.submit(self.declaration(self.patterned_image()))
        item = self.store.approve(self.store.verify(item["id"])["id"])
        approved = Path(item["approved_path"])
        approved.chmod(0o600)
        approved.write_bytes(b"changed")
        with self.assertRaises(Conflict):
            self.store.content_path(item["id"])

    def test_audit_table_rejects_mutation(self):
        item = self.store.submit(self.declaration(self.patterned_image()))
        with self.store.database.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM audit_events WHERE item_id = ?", (item["id"],))

    def test_declarative_checks_reject_code_and_unknown_actions(self):
        base = self.declaration("http://127.0.0.1:9999", "live_url")
        config = Config(data_dir=self.root / "live", allowed_origins=("http://127.0.0.1:9999",))
        store = Store(config)
        with self.assertRaises(StoreError):
            store.submit({**base, "checks": [{"action": "eval", "javascript": "alert(1)"}]})
        with self.assertRaises(StoreError):
            store.submit({**base, "checks": [{"action": "selector", "selector": "javascript:alert(1)"}]})

    def test_symlink_source_is_rejected(self):
        source = self.patterned_image()
        link = self.root / "linked.png"
        link.symlink_to(source)
        with self.assertRaises(StoreError):
            self.store.submit(self.declaration(link))

    def test_capacity_warns_at_80_and_rejects_at_90_percent(self):
        config = Config(data_dir=self.root / "small", volume_limit_bytes=1_000_000)
        store = Store(config)
        large = self.root / "large.bin"
        large.write_bytes(os.urandom(780_000))
        item = store.verify(store.submit(self.declaration(large, "file"))["id"])
        self.assertTrue(item["receipt"]["capacity"]["warning"])
        extra = self.root / "extra.bin"
        extra.write_bytes(os.urandom(160_000))
        with self.assertRaises(CapacityError):
            store.submit(self.declaration(extra, "file"))

    def test_pin_hash_and_rate_limit(self):
        encoded = hash_pin("1234")
        self.assertTrue(verify_pin("1234", encoded))
        self.assertFalse(verify_pin("654321", encoded))
        with self.assertRaises(ValueError):
            hash_pin("123")
        with self.assertRaises(ValueError):
            hash_pin("abcd")
        guard = PinGuard(self.store.database, encoded, max_attempts=2, window_seconds=60)
        with self.assertRaises(AuthError):
            guard.require("000000", "client")
        with self.assertRaises(AuthError):
            guard.require("000001", "client")
        with self.assertRaises(RateLimited):
            guard.require("1234", "client")

    def test_pin_change_persists_and_overrides_bootstrap_hash(self):
        old_hash = hash_pin("123456")
        guard = PinGuard(self.store.database, old_hash)
        guard.change("123456", "1234", "change-client")
        with self.assertRaises(AuthError):
            guard.require("123456", "old-client")
        guard.require("1234", "new-client")
        restarted_guard = PinGuard(self.store.database, old_hash)
        restarted_guard.require("1234", "restart-client")
        with self.store.database.connect() as connection:
            events = connection.execute(
                "SELECT event, actor, details_json FROM audit_events WHERE item_id = 'system'"
            ).fetchall()
        self.assertEqual([tuple(event) for event in events], [("pin_changed", "human", "{}")])

    def test_public_item_never_exposes_storage_or_source_paths(self):
        item = self.store.submit(self.declaration(self.patterned_image()))
        public = _public_item(item)
        self.assertNotIn("staged_path", public)
        self.assertNotIn("approved_path", public)
        self.assertNotIn("source", public)
        self.assertEqual(public["source_name"], "pattern.png")

    def test_movie_project_verification_and_pin_owned_edit_document(self):
        manifest = self.root / "edit.movie.json"
        manifest.write_text(json.dumps({
            "version": 1,
            "timeline": {
                "duration_seconds": 90,
                "frame_rate": 24,
                "width": 1920,
                "height": 1080,
                "color_space": "Rec. 709",
            },
            "clips": [],
            "markers": [{"type": "cut", "start_seconds": 12}],
        }), encoding="utf-8")
        item = self.store.verify(self.store.submit(self.declaration(manifest, "movie_project"))["id"])
        self.assertEqual(item["verification_status"], "ready", item["receipt"])
        self.assertEqual(item["receipt"]["metadata"]["frame_rate"], 24)
        item = self.store.approve(item["id"])
        self.assertEqual(self.store.movie_edit(item["id"])["revision"], 0)
        edit = {
            "version": 1,
            "title": "Agent trailer pass",
            "duration_seconds": 90,
            "frame_rate": 24,
            "resolution": {"width": 1920, "height": 1080},
            "color_space": "Rec. 709",
            "annotations": [{
                "id": "cut-12",
                "type": "cut",
                "start_seconds": 12,
                "end_seconds": 12,
                "label": "Tighten reaction",
                "notes": "Cut before the pause.",
            }],
        }
        saved = self.store.save_movie_edit(item["id"], edit, 0)
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["document"]["annotations"][0]["type"], "cut")
        with self.assertRaises(Conflict):
            self.store.save_movie_edit(item["id"], edit, 0)
        self.assertEqual([event["event"] for event in self.store.audit(item["id"])][-1], "movie_edit_saved")

    def test_invalid_movie_project_and_edit_ranges_are_rejected(self):
        invalid = self.root / "invalid.movie.json"
        invalid.write_text('{"version":1,"timeline":{}}', encoding="utf-8")
        item = self.store.verify(self.store.submit(self.declaration(invalid, "movie_project"))["id"])
        self.assertEqual(item["verification_status"], "rejected")

        valid = self.root / "valid.movie.json"
        valid.write_text(json.dumps({
            "version": 1,
            "timeline": {"duration_seconds": 30, "frame_rate": 30, "width": 1280, "height": 720},
        }), encoding="utf-8")
        approved = self.store.approve(self.store.verify(self.store.submit(self.declaration(valid, "movie_project"))["id"])["id"])
        with self.assertRaises(StoreError):
            self.store.save_movie_edit(approved["id"], {
                "version": 1,
                "title": "Bad range",
                "duration_seconds": 30,
                "frame_rate": 30,
                "resolution": {"width": 1280, "height": 720},
                "color_space": "Unknown",
                "annotations": [{
                    "id": "music-1", "type": "music", "start_seconds": 20,
                    "end_seconds": 10, "label": "Music", "notes": "",
                }],
            }, 0)

    def test_static_html_and_live_checks_capture_browser_evidence(self):
        html_dir = self.root / "site"
        html_dir.mkdir()
        (html_dir / "index.html").write_text("<style>body{background:#eef;color:#112}div{height:1200px;background:linear-gradient(#d55,#28c)}</style><h1>Static report</h1><div></div>", encoding="utf-8")
        static = self.store.verify(self.store.submit(self.declaration(html_dir, "html"))["id"])
        self.assertEqual(static["verification_status"], "ready")
        self.assertTrue((self.config.evidence_dir / static["id"] / "desktop.png").is_file())

        server = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            config = Config(data_dir=self.root / "browser", allowed_origins=(origin,))
            store = Store(config)
            declaration = self.declaration(origin, "live_url", checks=[
                {"action": "selector", "selector": "#title"},
                {"action": "click", "selector": "#change"},
                {"action": "text", "selector": "#status", "contains": "changed"},
                {"action": "scroll", "pixels": 700},
            ])
            live = store.verify(store.submit(declaration)["id"])
            self.assertEqual(live["verification_status"], "ready", live["receipt"])
            self.assertTrue((config.evidence_dir / live["id"] / "mobile.png").is_file())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_registered_novnc_and_model_registry_boundaries(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            targets = self.root / "targets.json"
            targets.write_text(json.dumps({"targets": [{"id": "android-test", "label": "Android test", "kind": "novnc", "url": origin}]}), encoding="utf-8")
            config = Config(data_dir=self.root / "novnc", allowed_origins=(origin,), targets_file=targets)
            store = Store(config)
            item = store.verify(store.submit(self.declaration("android-test", "novnc"))["id"])
            self.assertEqual(item["verification_status"], "ready", item["receipt"])
            with self.assertRaises(StoreError):
                store.submit(self.declaration("unregistered", "novnc"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        obj = self.root / "triangle.obj"
        obj.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
        model = self.store.verify(self.store.submit(self.declaration(obj, "model3d"))["id"])
        self.assertEqual(model["verification_status"], "ready")
        self.assertEqual(model["receipt"]["metadata"]["faces"], 1)

    def test_emulator_target_kind_is_reserved_and_not_submittable(self):
        origin = "http://127.0.0.1:6090"
        targets = self.root / "emulator-targets.json"
        targets.write_text(
            json.dumps({"targets": [{"id": "emu-reserved", "label": "Reserved emulator", "kind": "emulator", "url": origin}]}),
            encoding="utf-8",
        )
        config = Config(data_dir=self.root / "emulator", allowed_origins=(origin,), targets_file=targets)
        store = Store(config)
        self.assertEqual(config.targets()["emu-reserved"].kind, "emulator")
        with self.assertRaises(StoreError):
            store.submit(self.declaration("emu-reserved", "emulator"))
        with self.assertRaises(StoreError):
            store.submit(self.declaration("emu-reserved", "novnc"))

    def model(self, name: str, data: bytes, state: str = "candidate") -> dict:
        path = self.root / name
        path.write_bytes(data)
        item = self.store.verify(self.store.submit(self.declaration(path, "model3d"))["id"])
        self.assertEqual(item["verification_status"], "ready", item["receipt"])
        return self.store.approve(item["id"]) if state == "approved" else item

    def test_stl_and_3mf_models_verify(self):
        cube = cube_triangles(20)
        binary = self.model("cube.stl", binary_stl(cube))
        self.assertEqual(binary["receipt"]["metadata"], {"format": "stl", "encoding": "binary", "triangles": 12})
        text = "solid cube\n" + "".join(
            "facet normal 0 0 0\nouter loop\n" + "".join(f"vertex {x} {y} {z}\n" for x, y, z in triangle) + "endloop\nendfacet\n"
            for triangle in cube
        ) + "endsolid cube\n"
        self.assertEqual(self.model("text.stl", text.encode())["receipt"]["metadata"]["triangles"], 12)
        package = self.model("cube.3mf", three_mf(cube_triangles(10)))
        self.assertEqual(package["receipt"]["metadata"], {"format": "3mf", "models": 1, "vertices": 8, "triangles": 12})
        self.assertIn("3MF structure was parsed", package["receipt"]["proved"][0])
        for name, data, error in [
            ("short.stl", binary_stl(cube)[:-10], "neither a binary nor a text STL"),
            ("empty.stl", b"solid empty\nendsolid empty\n", "STL has no triangles"),
            ("broken.3mf", b"PK not really a zip", "3MF package could not be read"),
            ("flat.3mf", three_mf([]), "3MF has no triangles"),
            ("cube.fbx", b"Kaydara FBX Binary", "3D source must be GLB, GLTF, OBJ, STL or 3MF"),
        ]:
            path = self.root / name
            path.write_bytes(data)
            item = self.store.verify(self.store.submit(self.declaration(path, "model3d"))["id"])
            self.assertEqual(item["verification_status"], "rejected", name)
            self.assertIn(error, " ".join(item["receipt"]["errors"]), name)

    def test_3d_model_notes_keep_the_camera_view(self):
        model = self.model("cube.glb", cube_glb())
        view = {"position": [0.1, 0.05, 0.12], "target": [0, 0, 0], "mode": "clay"}
        note = self.store.add_note(model["id"], 0, "Wing too thick", None, view=view)
        self.assertEqual((note["view"], note["at_seconds"]), (view, 0.0))
        animated = self.store.add_note(model["id"], 1.25, "Pose at 1.25 s", None, view={**view, "mode": "textured", "clip": "spin"})
        self.assertEqual(animated["view"]["clip"], "spin")
        self.assertEqual([value["id"] for value in self.store.notes(model["id"])], [note["id"], animated["id"]])
        for bad in [None, {"position": [0, 0], "target": [0, 0, 0], "mode": "clay"}, {**view, "mode": "x-ray"},
                    {**view, "target": [0, "a", 0]}, {**view, "clip": 3}]:
            with self.assertRaises(StoreError, msg=bad):
                self.store.add_note(model["id"], 0, "x", None, view=bad)
        video = self.root / "clip.mp4"
        video.write_bytes(b"not checked here")
        movie = self.store.submit(self.declaration(video, "video"))
        with self.assertRaises(Conflict):
            self.store.add_note(movie["id"], 0, "x", None, view=view)
        self.store.hide(self.store.approve(model["id"])["id"])
        with self.assertRaises(Conflict):
            self.store.add_note(model["id"], 0, "hidden", None, view=view)
        self.store.delete(model["id"])
        with self.assertRaises(NotFound):
            self.store.notes(model["id"])

    def test_slicer_opens_print_files_and_stores_stl_copies_of_gltf(self):
        stl = self.model("part.stl", binary_stl(cube_triangles(20)))
        path, location = self.store.slicer_target(stl["id"])
        self.assertEqual((path, location), (self.root / "part.stl", "original"))
        (self.root / "part.stl").unlink()
        path, location = self.store.slicer_target(stl["id"])
        self.assertEqual(location, "copy")
        self.assertEqual(path.read_bytes()[80:84], (12).to_bytes(4, "little"))
        glb = self.model("part.glb", cube_glb())
        with self.assertRaisesRegex(Conflict, "STL copy"):
            self.store.slicer_target(glb["id"])
        copy = self.store.save_slicer_copy(glb["id"], binary_stl(cube_triangles(50)))
        self.assertEqual(copy, self.config.slicer_dir / f"{glb['id']}.stl")
        self.assertEqual(len(copy.read_bytes()), 84 + 50 * 12)
        for bad in [b"", b"x" * 84, binary_stl(cube_triangles(50))[:-1]]:
            with self.assertRaises(StoreError):
                self.store.save_slicer_copy(glb["id"], bad)
        image = self.store.submit(self.declaration(self.patterned_image()))
        with self.assertRaises(Conflict):
            self.store.save_slicer_copy(image["id"], binary_stl(cube_triangles(5)))
        self.store.delete(glb["id"])
        self.assertFalse(copy.exists())

    def test_note_views_survive_a_database_made_before_them(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        with closing(sqlite3.connect(legacy / "previewer.sqlite3")) as connection, connection:
            connection.execute("CREATE TABLE movie_notes (id TEXT PRIMARY KEY, item_id TEXT NOT NULL, at_seconds REAL NOT NULL, text TEXT NOT NULL, has_screenshot INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
            connection.execute("INSERT INTO movie_notes VALUES ('n1', 'i1', 2.0, 'old note', 0, '2026-10-26T00:00:00+00:00')")
        Store(Config(data_dir=legacy))
        with closing(sqlite3.connect(legacy / "previewer.sqlite3")) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(movie_notes)")]
            self.assertIn("view_json", columns)
            self.assertEqual(connection.execute("SELECT text, view_json FROM movie_notes").fetchone(), ("old note", None))


class PdfFrameTests(unittest.TestCase):
    """Firefox's PDF viewer draws no pages in a frame that forbids scripts."""

    def test_pdf_frames_allow_scripts_but_stay_cross_origin(self):
        app = (Path(__file__).resolve().parents[1] / "previewer" / "static" / "app.js").read_text(encoding="utf-8")
        line = next(line for line in app.splitlines() if "frame.sandbox = item.kind" in line)
        self.assertIn('item.kind === "pdf" ? "allow-scripts"', line)
        self.assertNotIn("allow-same-origin", line)


if __name__ == "__main__":
    unittest.main()
