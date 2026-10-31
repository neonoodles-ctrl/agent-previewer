from __future__ import annotations

import html
import json
import mimetypes
import re
import shutil
import statistics
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

from PIL import Image, ImageStat

from .config import Config, normalized_origin


class VerificationFailure(ValueError):
    pass


@dataclass
class VerificationResult:
    status: str
    proved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "proved": self.proved,
            "warnings": self.warnings,
            "evidence": self.evidence,
            "metadata": self.metadata,
            "errors": self.errors,
            "human_approval": False,
            "publicly_published": False,
        }


class Verifier:
    def __init__(self, config: Config):
        self.config = config

    def verify(self, item: dict, evidence_dir: Path) -> VerificationResult:
        evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        kind = item["kind"]
        source = Path(item["staged_path"]) if item.get("staged_path") else None
        try:
            if kind == "image":
                return self._image(source, evidence_dir)
            if kind in {"video", "audio"}:
                return self._media(source, evidence_dir, kind)
            if kind == "movie_project":
                return self._movie_project(source)
            if kind in {"markdown", "report", "log", "subtitle"}:
                return self._text(source, evidence_dir, kind)
            if kind == "pdf":
                return self._pdf(source, evidence_dir)
            if kind == "html":
                return self._html(source, evidence_dir)
            if kind == "live_url":
                return self._live(item["target_url"], item["checks"], evidence_dir)
            if kind == "novnc":
                return self._novnc(item["target_url"], evidence_dir)
            if kind in {"model3d", "blend"}:
                return self._model(item, source, evidence_dir)
            if kind == "apk":
                return self._apk(item, source, evidence_dir)
            return self._generic(source)
        except VerificationFailure as exc:
            return VerificationResult(status="rejected", errors=[str(exc)])
        except Exception as exc:
            return VerificationResult(status="error", errors=[f"verifier error: {type(exc).__name__}: {exc}"])

    def _image(self, source: Path | None, evidence_dir: Path) -> VerificationResult:
        path = _required_file(source)
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
                if image.width < 2 or image.height < 2:
                    raise VerificationFailure("image dimensions are too small")
                if _is_blank(image):
                    raise VerificationFailure("image is visually blank")
                thumb = image.convert("RGB")
                thumb.thumbnail((1280, 900))
                output = evidence_dir / "thumbnail.jpg"
                thumb.save(output, quality=88)
                metadata = {"width": image.width, "height": image.height, "format": image.format}
        except VerificationFailure:
            raise
        except Exception as exc:
            raise VerificationFailure(f"image decode failed: {exc}") from exc
        return VerificationResult(
            status="ready",
            proved=["The image decoded successfully and contains visible variation."],
            evidence=[output.name],
            metadata=metadata,
        )

    def _media(self, source: Path | None, evidence_dir: Path, kind: str) -> VerificationResult:
        path = _required_file(source)
        probe = _run_json([
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
        ])
        streams = probe.get("streams", [])
        wanted = "video" if kind == "video" else "audio"
        matching = [stream for stream in streams if stream.get("codec_type") == wanted]
        if not matching:
            raise VerificationFailure(f"no decodable {wanted} stream was found")
        duration = float(probe.get("format", {}).get("duration") or matching[0].get("duration") or 0)
        if duration <= 0:
            raise VerificationFailure("media duration is zero")
        if kind == "video":
            output = evidence_dir / "frame.jpg"
            seek = min(max(duration * 0.25, 0.05), max(duration - 0.05, 0.05))
            _run(["ffmpeg", "-y", "-v", "error", "-ss", str(seek), "-i", str(path), "-frames:v", "1", str(output)])
            with Image.open(output) as frame:
                if _is_blank(frame):
                    raise VerificationFailure("sampled video frame is visually blank")
            proved = ["The video decoded, has positive duration, and produced a visible sampled frame."]
        else:
            output = evidence_dir / "waveform.png"
            _run([
                "ffmpeg", "-y", "-v", "error", "-i", str(path), "-filter_complex",
                "aformat=channel_layouts=mono,showwavespic=s=1200x260:colors=2dd4bf", "-frames:v", "1", str(output)
            ])
            with Image.open(output) as waveform:
                if _is_blank(waveform):
                    raise VerificationFailure("audio decoded but produced a blank waveform")
            proved = ["The audio decoded, has positive duration, and produced a non-blank waveform."]
        format_data = probe.get("format", {})
        stream = matching[0]
        metadata = {
            "duration_seconds": duration,
            "format_name": format_data.get("format_long_name") or format_data.get("format_name"),
            "bit_rate": _optional_int(format_data.get("bit_rate")),
        }
        if kind == "video":
            metadata["video"] = {
                "codec": stream.get("codec_long_name") or stream.get("codec_name"),
                "codec_tag": stream.get("codec_tag_string"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "frame_rate": _parse_rate(stream.get("avg_frame_rate") or stream.get("r_frame_rate")),
                "pixel_format": stream.get("pix_fmt"),
                "color_space": stream.get("color_space") or stream.get("color_primaries"),
            }
            audio = next((value for value in streams if value.get("codec_type") == "audio"), None)
            if audio:
                metadata["audio"] = _audio_metadata(audio)
        else:
            metadata["audio"] = _audio_metadata(stream)
        return VerificationResult(status="ready", proved=proved, evidence=[output.name], metadata=metadata)

    def _movie_project(self, source: Path | None) -> VerificationResult:
        path = _required_file(source)
        if path.stat().st_size > 2 * 1024 * 1024:
            raise VerificationFailure("movie project JSON exceeds 2 MiB")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerificationFailure("movie project must be valid UTF-8 JSON") from exc
        if not isinstance(document, dict) or document.get("version") != 1:
            raise VerificationFailure("movie project must be a version 1 JSON object")
        timeline = document.get("timeline")
        if not isinstance(timeline, dict):
            raise VerificationFailure("movie project requires timeline metadata")
        duration = _manifest_number(timeline.get("duration_seconds"), "timeline duration", 0.01, 86_400)
        frame_rate = _manifest_number(timeline.get("frame_rate"), "timeline frame rate", 1, 240)
        width = timeline.get("width")
        height = timeline.get("height")
        if not isinstance(width, int) or isinstance(width, bool) or not 1 <= width <= 16_384:
            raise VerificationFailure("timeline width must be between 1 and 16384")
        if not isinstance(height, int) or isinstance(height, bool) or not 1 <= height <= 16_384:
            raise VerificationFailure("timeline height must be between 1 and 16384")
        clips = document.get("clips", [])
        markers = document.get("markers", [])
        if not isinstance(clips, list) or len(clips) > 200:
            raise VerificationFailure("movie project clips must be a list with at most 200 entries")
        if not isinstance(markers, list) or len(markers) > 500:
            raise VerificationFailure("movie project markers must be a list with at most 500 entries")
        for clip in clips:
            if not isinstance(clip, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(clip.get("item_id", ""))):
                raise VerificationFailure("each project clip must reference a Previewer item ID")
            start = _manifest_number(clip.get("start_seconds", 0), "clip start", 0, duration)
            end = _manifest_number(clip.get("end_seconds", duration), "clip end", 0, duration)
            if end <= start:
                raise VerificationFailure("project clip end must be after its start")
        for marker in markers:
            if not isinstance(marker, dict) or marker.get("type") not in {"cut", "music", "clip", "note"}:
                raise VerificationFailure("project markers must use cut, music, clip, or note types")
            start = _manifest_number(marker.get("start_seconds", 0), "marker start", 0, duration)
            if marker["type"] in {"music", "clip"}:
                end = _manifest_number(marker.get("end_seconds"), "marker end", 0, duration)
                if end <= start:
                    raise VerificationFailure("range marker end must be after its start")
        return VerificationResult(
            status="ready",
            proved=["The version 1 movie project manifest and timeline metadata were parsed successfully."],
            metadata={
                "duration_seconds": duration,
                "frame_rate": frame_rate,
                "width": width,
                "height": height,
                "color_space": timeline.get("color_space") or "Unknown",
                "clip_count": len(clips),
                "marker_count": len(markers),
            },
        )

    def _text(self, source: Path | None, evidence_dir: Path, kind: str) -> VerificationResult:
        path = _required_file(source)
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise VerificationFailure("text is not valid UTF-8") from exc
        if not content.strip():
            raise VerificationFailure("text content is blank")
        document = _render_text_document(content, kind, path.name)
        rendered = evidence_dir / "first-page.html"
        rendered.write_text(document, encoding="utf-8")
        screenshot = evidence_dir / "first-page.png"
        browser = BrowserVerifier(self.config)
        browser.capture_file(rendered, screenshot)
        _require_nonblank_image(screenshot, "rendered first page is visually blank")
        return VerificationResult(
            status="ready",
            proved=["The UTF-8 text is non-empty and its first page rendered visibly."],
            evidence=[rendered.name, screenshot.name],
            metadata={"characters": len(content), "lines": content.count("\n") + 1},
        )

    def _pdf(self, source: Path | None, evidence_dir: Path) -> VerificationResult:
        path = _required_file(source)
        prefix = evidence_dir / "first-page"
        _run(["pdftoppm", "-f", "1", "-singlefile", "-png", "-r", "120", str(path), str(prefix)])
        output = evidence_dir / "first-page.png"
        _require_nonblank_image(output, "PDF first page is visually blank")
        with Image.open(output) as image:
            dimensions = {"render_width": image.width, "render_height": image.height}
        return VerificationResult(
            status="ready",
            proved=["The PDF decoded and its first page rendered visibly."],
            evidence=[output.name], metadata=dimensions,
        )

    def _html(self, source: Path | None, evidence_dir: Path) -> VerificationResult:
        path = _html_entry(source)
        output = evidence_dir / "desktop.png"
        browser = BrowserVerifier(self.config)
        report = browser.capture_file(path, output)
        _require_nonblank_image(output, "static HTML rendered a blank page")
        return VerificationResult(
            status="ready",
            proved=["The static HTML opened in an isolated browser context and rendered visibly."],
            warnings=report["warnings"], evidence=[output.name], metadata=report,
        )

    def _live(self, url: str, checks: list[dict], evidence_dir: Path) -> VerificationResult:
        report = BrowserVerifier(self.config).capture_live(url, checks, evidence_dir)
        return VerificationResult(
            status="ready",
            proved=report.pop("proved"),
            warnings=report.pop("warnings"),
            evidence=["desktop.png", "mobile.png"],
            metadata=report,
        )

    def _novnc(self, url: str, evidence_dir: Path) -> VerificationResult:
        report = BrowserVerifier(self.config).capture_live(
            url, [{"action": "selector", "selector": "canvas"}], evidence_dir, frame_selector="canvas"
        )
        _require_nonblank_image(evidence_dir / "remote-frame.png", "noVNC target returned a blank frame")
        return VerificationResult(
            status="ready",
            proved=["The registered noVNC page responded and exposed a non-blank canvas frame."],
            warnings=report.get("warnings", []), evidence=["remote-frame.png", "desktop.png", "mobile.png"], metadata=report,
        )

    def _model(self, item: dict, source: Path | None, evidence_dir: Path) -> VerificationResult:
        path = _required_file(source) if item["kind"] == "blend" else _model_entry(source)
        suffix = path.suffix.lower()
        if item["kind"] == "blend":
            if not item.get("target_url"):
                raise VerificationFailure(".blend files require an attached registered noVNC target")
            remote = self._novnc(item["target_url"], evidence_dir)
            remote.metadata["blend_size_bytes"] = path.stat().st_size
            remote.proved.insert(0, "The .blend file exists and has a registered noVNC review target.")
            return remote
        if suffix not in {".glb", ".gltf", ".obj", ".stl", ".3mf"}:
            raise VerificationFailure("3D source must be GLB, GLTF, OBJ, STL or 3MF")
        metadata = _inspect_model(path)
        return VerificationResult(
            status="ready",
            proved=[f"The {suffix[1:].upper()} structure was parsed and is available to the local 3D viewer."],
            metadata=metadata,
        )

    def _apk(self, item: dict, source: Path | None, evidence_dir: Path) -> VerificationResult:
        path = _required_file(source)
        if not item.get("target_url"):
            raise VerificationFailure("APK submissions require an attached registered noVNC target")
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                if "AndroidManifest.xml" not in names:
                    raise VerificationFailure("APK has no AndroidManifest.xml")
                icons = [name for name in names if name.lower().endswith(".png") and "mipmap" in name.lower()]
                icon_name = icons[0] if icons else None
                if icon_name:
                    info = archive.getinfo(icon_name)
                    if info.file_size > 20 * 1024 * 1024:
                        raise VerificationFailure("APK icon exceeds the 20 MiB inspection limit")
                    icon_path = evidence_dir / "apk-icon.png"
                    icon_path.write_bytes(archive.read(icon_name))
                    with Image.open(icon_path) as icon:
                        icon.verify()
        except zipfile.BadZipFile as exc:
            raise VerificationFailure("APK is not a valid ZIP container") from exc
        remote = self._novnc(item["target_url"], evidence_dir)
        remote.proved.insert(0, "The APK container and Android manifest were inspected successfully.")
        remote.metadata.update({"archive_entries": len(names), "icon_entry": icon_name})
        if icon_name:
            remote.evidence.append("apk-icon.png")
        return remote

    def _generic(self, source: Path | None) -> VerificationResult:
        path = _required_file(source)
        if path.stat().st_size == 0:
            raise VerificationFailure("file is empty")
        return VerificationResult(
            status="ready", proved=["The file was copied, hashed, and is non-empty."],
            metadata={"size_bytes": path.stat().st_size, "mime": mimetypes.guess_type(path.name)[0]},
        )


class BrowserVerifier:
    def __init__(self, config: Config):
        self.config = config

    def _playwright(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise VerificationFailure("Playwright Python is not installed") from exc
        return sync_playwright()

    def _launch(self, playwright):
        executable = self.config.chromium_path
        if executable and not Path(executable).is_file():
            raise VerificationFailure(f"Chromium executable not found: {executable}")
        return playwright.chromium.launch(headless=True, executable_path=executable or None)

    def capture_file(self, source: Path, output: Path) -> dict:
        console_errors: list[str] = []
        page_errors: list[str] = []
        blocked_requests: list[str] = []
        with self._playwright() as playwright:
            browser = self._launch(playwright)
            context = browser.new_context(viewport={"width": 1440, "height": 900}, java_script_enabled=True)
            allowed_root = (source if source.is_dir() else source.parent).resolve()
            def isolate(route):
                parsed = urlsplit(route.request.url)
                scheme = parsed.scheme
                file_allowed = False
                if scheme == "file":
                    try:
                        # url2pathname reads file:///C:/... on Windows as well as file:///home/... here.
                        from urllib.request import url2pathname
                        file_allowed = Path(url2pathname(parsed.path)).resolve().is_relative_to(allowed_root)
                    except (OSError, ValueError):
                        file_allowed = False
                if file_allowed or scheme in {"data", "blob", "about"}:
                    route.continue_()
                else:
                    blocked_requests.append(route.request.url)
                    route.abort("blockedbyclient")
            context.route("**/*", isolate)
            page = context.new_page()
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            response = page.goto(source.resolve().as_uri(), wait_until="load", timeout=self.config.verification_timeout_ms)
            page.screenshot(path=str(output), full_page=True, animations="disabled")
            status = response.status if response else None
            context.close()
            browser.close()
        warnings = [f"console: {message}" for message in console_errors] + [f"page: {message}" for message in page_errors]
        if blocked_requests:
            warnings.append(f"{len(blocked_requests)} external request(s) were blocked by the static sandbox")
        return {"http_status": status, "console_errors": console_errors, "page_errors": page_errors, "blocked_requests": blocked_requests, "warnings": warnings}

    def capture_live(self, url: str, checks: list[dict], evidence_dir: Path, frame_selector: str | None = None) -> dict:
        _require_allowed_url(url, self.config.allowed_origins)
        console_errors: list[str] = []
        page_errors: list[str] = []
        failed_requests: list[dict] = []
        bad_responses: list[dict] = []
        blocked_requests: list[str] = []
        proved = ["The allow-listed URL responded in desktop and mobile viewports."]
        final_url = url
        route_checks = [check for check in checks if check["action"] == "route"]
        if route_checks:
            final_url = urljoin(url.rstrip("/") + "/", route_checks[-1]["path"].lstrip("/"))
            _require_allowed_url(final_url, self.config.allowed_origins)
        with self._playwright() as playwright:
            browser = self._launch(playwright)
            for label, viewport in (("desktop", {"width": 1440, "height": 900}), ("mobile", {"width": 390, "height": 844})):
                context = browser.new_context(viewport=viewport, java_script_enabled=True)

                def route_request(route):
                    request_url = route.request.url
                    scheme = urlsplit(request_url).scheme
                    try:
                        allowed = scheme in {"data", "blob", "about"} or normalized_origin(request_url) in self.config.allowed_origins
                    except ValueError:
                        allowed = False
                    if allowed:
                        route.continue_()
                    else:
                        blocked_requests.append(request_url)
                        route.abort("blockedbyclient")

                context.route("**/*", route_request)
                page = context.new_page()
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("requestfailed", lambda request: failed_requests.append({"url": request.url, "failure": str(request.failure)}))
                page.on("response", lambda response: bad_responses.append({"url": response.url, "status": response.status}) if response.status >= 400 else None)
                response = page.goto(final_url, wait_until="domcontentloaded", timeout=self.config.verification_timeout_ms)
                if not response or response.status >= 400:
                    raise VerificationFailure(f"live URL returned HTTP {response.status if response else 'no response'}")
                page.wait_for_load_state("load", timeout=self.config.verification_timeout_ms)
                if label == "desktop":
                    for check in checks:
                        action = check["action"]
                        if action == "selector":
                            page.locator(check["selector"]).first.wait_for(state="visible", timeout=self.config.verification_timeout_ms)
                            proved.append(f"Visible selector found: {check['selector']}")
                        elif action == "click":
                            page.locator(check["selector"]).first.click(timeout=self.config.verification_timeout_ms)
                            proved.append(f"Clicked declared selector: {check['selector']}")
                        elif action == "text":
                            actual = page.locator(check["selector"]).first.inner_text(timeout=self.config.verification_timeout_ms)
                            if check["contains"] not in actual:
                                raise VerificationFailure(f"declared text was not found in {check['selector']}")
                            proved.append(f"Declared text found in selector: {check['selector']}")
                        elif action == "scroll":
                            before = page.evaluate("window.scrollY")
                            page.mouse.wheel(0, check["pixels"])
                            page.wait_for_timeout(150)
                            after = page.evaluate("window.scrollY")
                            if check["pixels"] > 0 and after <= before:
                                raise VerificationFailure("declared scroll did not move the page")
                            proved.append("The page responded to the declared scroll check.")
                    if frame_selector:
                        page.locator(frame_selector).first.screenshot(path=str(evidence_dir / "remote-frame.png"), animations="disabled")
                output = evidence_dir / f"{label}.png"
                page.screenshot(path=str(output), animations="disabled")
                _require_nonblank_image(output, f"{label} live screenshot is visually blank")
                context.close()
            browser.close()
        warnings = []
        if console_errors:
            warnings.append(f"{len(console_errors)} console error(s) were captured")
        if page_errors:
            warnings.append(f"{len(page_errors)} uncaught page error(s) were captured")
        if failed_requests or bad_responses:
            warnings.append(f"{len(failed_requests) + len(bad_responses)} failed network response(s) were captured")
        if blocked_requests:
            warnings.append(f"{len(blocked_requests)} request(s) outside the allow-list were blocked")
        return {
            "proved": proved,
            "warnings": warnings,
            "final_url": final_url,
            "console_errors": console_errors,
            "page_errors": page_errors,
            "failed_requests": failed_requests,
            "bad_responses": bad_responses,
            "blocked_requests": blocked_requests,
        }


def _required_file(source: Path | None) -> Path:
    if not source or not source.is_file():
        raise VerificationFailure("staged source file is missing")
    return source


def _html_entry(source: Path | None) -> Path:
    if not source:
        raise VerificationFailure("staged HTML source is missing")
    entry = source / "index.html" if source.is_dir() else source
    if not entry.is_file():
        raise VerificationFailure("HTML submission requires a file or directory containing index.html")
    return entry


def _model_entry(source: Path | None) -> Path:
    if not source:
        raise VerificationFailure("staged 3D source is missing")
    if source.is_file():
        return source
    entries = list(source.glob("*.gltf")) if source.is_dir() else []
    if len(entries) != 1:
        raise VerificationFailure("3D directory requires exactly one top-level GLTF file")
    return entries[0]


def _is_blank(image: Image.Image) -> bool:
    sample = image.convert("RGB")
    sample.thumbnail((160, 160))
    stat = ImageStat.Stat(sample)
    luminance = statistics.fmean(stat.mean)
    variation = sum(stat.var)
    return variation < 1.0 or (luminance < 1.0 and variation < 6.0)


def _require_nonblank_image(path: Path, message: str) -> None:
    if not path.is_file():
        raise VerificationFailure(message)
    with Image.open(path) as image:
        image.load()
        if _is_blank(image):
            raise VerificationFailure(message)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    missing = shutil.which(command[0])
    if not missing:
        raise VerificationFailure(f"required verifier tool is missing: {command[0]}")
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        raise VerificationFailure(f"{command[0]} failed: {detail[0] if detail else 'unknown error'}")
    return result


def _run_json(command: list[str]) -> dict:
    result = _run(command)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VerificationFailure(f"{command[0]} returned invalid metadata") from exc


def _parse_rate(value: object) -> float | None:
    try:
        text = str(value)
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            rate = float(numerator) / float(denominator)
        else:
            rate = float(text)
        return round(rate, 3) if rate > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _audio_metadata(stream: dict) -> dict:
    return {
        "codec": stream.get("codec_long_name") or stream.get("codec_name"),
        "sample_rate": _optional_int(stream.get("sample_rate")),
        "channels": stream.get("channels"),
        "channel_layout": stream.get("channel_layout"),
    }


def _manifest_number(value: object, name: str, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise VerificationFailure(f"{name} must be a number")
    number = float(value)
    if number != number or number in {float("inf"), float("-inf")} or not minimum <= number <= maximum:
        raise VerificationFailure(f"{name} must be between {minimum} and {maximum}")
    return round(number, 6)


def _render_text_document(content: str, kind: str, title: str) -> str:
    escaped_title = html.escape(title)
    if kind in {"markdown", "report"}:
        blocks: list[str] = []
        in_code = False
        for raw_line in content.splitlines():
            line = html.escape(raw_line)
            if raw_line.startswith("```"):
                blocks.append("</code></pre>" if in_code else "<pre><code>")
                in_code = not in_code
            elif in_code:
                blocks.append(line + "\n")
            elif raw_line.startswith("### "):
                blocks.append(f"<h3>{line[4:]}</h3>")
            elif raw_line.startswith("## "):
                blocks.append(f"<h2>{line[3:]}</h2>")
            elif raw_line.startswith("# "):
                blocks.append(f"<h1>{line[2:]}</h1>")
            elif raw_line.startswith(("- ", "* ")):
                blocks.append(f"<p class='bullet'>• {line[2:]}</p>")
            elif raw_line:
                blocks.append(f"<p>{line}</p>")
            else:
                blocks.append("<br>")
        if in_code:
            blocks.append("</code></pre>")
        body = "".join(blocks)
    else:
        body = f"<pre>{html.escape(content)}</pre>"
    return f"""<!doctype html><meta charset=utf-8><title>{escaped_title}</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:920px;margin:0 auto;padding:56px;color:#17212b;background:#f7f4ed}}h1,h2,h3{{line-height:1.15}}pre{{white-space:pre-wrap;background:#e9e5da;padding:20px;border-radius:12px}}.bullet{{padding-left:16px}}</style>
<main><small>PREVIEWER RENDER</small>{body}</main>"""


def _require_allowed_url(url: str, allowed_origins: tuple[str, ...]) -> None:
    if normalized_origin(url) not in allowed_origins:
        raise VerificationFailure("URL origin is not allow-listed")


def _inspect_model(path: Path) -> dict:
    suffix = path.suffix.lower()
    if suffix == ".obj":
        vertices = faces = 0
        for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
            vertices += line.startswith("v ")
            faces += line.startswith("f ")
        if vertices < 3 or faces < 1:
            raise VerificationFailure("OBJ has no renderable mesh")
        return {"format": "obj", "vertices": vertices, "faces": faces}
    if suffix == ".stl":
        return _inspect_stl(path)
    if suffix == ".3mf":
        return _inspect_3mf(path)
    if suffix == ".gltf":
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("asset", {}).get("version") != "2.0" or not document.get("meshes"):
            raise VerificationFailure("GLTF has no renderable 2.0 mesh")
        return {"format": "gltf", "meshes": len(document["meshes"]), "scenes": len(document.get("scenes", []))}
    data = path.read_bytes()
    if len(data) < 20 or data[:4] != b"glTF" or int.from_bytes(data[4:8], "little") != 2:
        raise VerificationFailure("GLB header is invalid or unsupported")
    declared = int.from_bytes(data[8:12], "little")
    if declared != len(data):
        raise VerificationFailure("GLB declared length does not match the file")
    return {"format": "glb", "version": 2, "size_bytes": len(data)}


def _inspect_stl(path: Path) -> dict:
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(84)
    # A binary STL may also start with "solid", so the exact size decides which kind it is.
    if len(head) == 84 and size == 84 + 50 * int.from_bytes(head[80:84], "little"):
        triangles = int.from_bytes(head[80:84], "little")
        if not triangles:
            raise VerificationFailure("STL has no triangles")
        return {"format": "stl", "encoding": "binary", "triangles": triangles}
    if not head.lstrip().lower().startswith(b"solid"):
        raise VerificationFailure("STL is neither a binary nor a text STL")
    triangles = 0
    with path.open("rb") as handle:
        for line in handle:
            triangles += line.lstrip().startswith(b"facet")
    if not triangles:
        raise VerificationFailure("STL has no triangles")
    return {"format": "stl", "encoding": "text", "triangles": triangles}


def _inspect_3mf(path: Path) -> dict:
    vertices = triangles = 0
    try:
        with zipfile.ZipFile(path) as archive:
            models = [name for name in archive.namelist() if name.lower().endswith(".model")]
            if not models:
                raise VerificationFailure("3MF has no 3D model part")
            for name in models:
                with archive.open(name) as stream:
                    for _, node in ElementTree.iterparse(stream):
                        tag = node.tag.rsplit("}", 1)[-1]
                        vertices += tag == "vertex"
                        triangles += tag == "triangle"
                        node.clear()
    except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise VerificationFailure("3MF package could not be read") from exc
    if not triangles:
        raise VerificationFailure("3MF has no triangles")
    return {"format": "3mf", "models": len(models), "vertices": vertices, "triangles": triangles}
