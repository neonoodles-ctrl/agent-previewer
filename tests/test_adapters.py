from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

from previewer.adapters import previewer_item_url
from previewer.cli import main
from previewer.config import Config
from previewer.security import hash_pin
from previewer.service import build_server


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_pattern_image(self, name="pattern.png") -> Path:
        image = Image.new("RGB", (64, 64))
        pixels = image.load()
        for y in range(64):
            for x in range(64):
                pixels[x, y] = (230, 70, 60) if (x // 8 + y // 8) % 2 else (30, 90, 210)
        path = self.root / name
        image.save(path)
        return path

    def test_service_headers_and_missing_pin(self):
        data_dir = self.root / "data"
        config = Config(data_dir=data_dir, pin_hash=hash_pin("123456"), volume_limit_bytes=5 * 1024 * 1024)
        server = build_server(config, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urlopen(base + "/") as response:
                self.assertEqual(response.headers.get("X-Frame-Options"), "SAMEORIGIN")
                self.assertIn("frame-ancestors 'self'", response.headers.get("Content-Security-Policy", ""))
            request = Request(
                base + "/api/human/action",
                data=json.dumps({"item_id": "a" * 32, "action": "approve"}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as caught:
                urlopen(request)
            self.assertEqual(caught.exception.code, 404)  # no PIN needed; unknown item is not found
            caught.exception.close()
            image = self.write_pattern_image()
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main([
                    "--url", base,
                    "submit", str(image),
                    "--project-run-id", "project/cli",
                    "--title", image.name,
                    "--kind", "image",
                    "--expected-result", "A visible checkerboard image",
                ])
            self.assertEqual(code, 0)
            item = json.loads(buffer.getvalue())
            self.assertEqual(item["verification_status"], "ready")
            self.assertEqual(item["state"], "candidate")
            self.assertNotIn("staged_path", item)
            self.assertNotIn("source", item)
            self.assertEqual(item["project_run_id"], "project/cli")
            self.assertEqual(previewer_item_url(base, item["id"]), f"{base}/?item={item['id']}")
            audit_request = Request(base + f"/api/items/{item['id']}/audit")
            with urlopen(audit_request) as response:
                events = [event["event"] for event in json.load(response)["events"]]
            self.assertEqual(events, ["submitted", "verification_ready"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_open_does_not_launch_browser_by_default(self):
        from unittest.mock import patch

        with patch("previewer.cli.webbrowser.open") as opened:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                with patch.dict(os.environ, {"PREVIEWER_OPEN_BROWSER": ""}, clear=False):
                    code = main(["open"])
            self.assertEqual(code, 0)
            opened.assert_not_called()
            self.assertIn("http://127.0.0.1:8087", buffer.getvalue())

        with patch("previewer.cli.webbrowser.open", return_value=True) as opened:
            code = main(["open", "--browser"])
            self.assertEqual(code, 0)
            opened.assert_called_once()


if __name__ == "__main__":
    unittest.main()
