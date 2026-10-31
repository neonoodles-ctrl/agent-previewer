from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def default_chromium() -> str | None:
    """Linux distributions install Chromium here; elsewhere Playwright's own Chromium is used."""
    return "/usr/bin/chromium" if Path("/usr/bin/chromium").is_file() else None


@dataclass(frozen=True)
class Target:
    id: str
    label: str
    kind: str
    url: str


@dataclass(frozen=True)
class Config:
    data_dir: Path
    pin_hash: str = ""
    allowed_origins: tuple[str, ...] = ()
    targets_file: Path | None = None
    volume_limit_bytes: int = 10 * 1024 * 1024 * 1024
    # None means Playwright's own Chromium (`playwright install chromium`), as on Windows.
    chromium_path: str | None = field(default_factory=lambda: default_chromium())
    verification_timeout_ms: int = 15_000
    rate_limit_attempts: int = 5
    rate_limit_window_seconds: int = 300
    # Open in slicer launches this, a path or a command on PATH; PrusaSlicer reads STL, 3MF and OBJ.
    slicer_path: str = "prusa-slicer"
    static_dir: Path = field(default_factory=lambda: Path(__file__).with_name("static"))

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(os.environ.get("PREVIEWER_DATA_DIR", "./data")).expanduser().resolve()
        targets = os.environ.get("PREVIEWER_TARGETS_FILE")
        origins = tuple(
            normalized_origin(value.strip())
            for value in os.environ.get("PREVIEWER_ALLOWED_ORIGINS", "").split(",")
            if value.strip()
        )
        return cls(
            data_dir=data_dir,
            pin_hash=os.environ.get("PREVIEWER_PIN_HASH", ""),
            allowed_origins=origins,
            targets_file=Path(targets).expanduser().resolve() if targets else None,
            volume_limit_bytes=int(os.environ.get("PREVIEWER_VOLUME_LIMIT_BYTES", str(10 * 1024**3))),
            chromium_path=os.environ.get("PREVIEWER_CHROMIUM") or default_chromium(),
            verification_timeout_ms=int(os.environ.get("PREVIEWER_VERIFY_TIMEOUT_MS", "15000")),
            slicer_path=os.environ.get("PREVIEWER_SLICER", cls.slicer_path),
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "previewer.sqlite3"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def approved_dir(self) -> Path:
        return self.data_dir / "approved"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def notes_dir(self) -> Path:
        return self.data_dir / "notes"

    @property
    def slicer_dir(self) -> Path:
        return self.data_dir / "slicer"

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (self.staging_dir, self.approved_dir, self.evidence_dir, self.notes_dir, self.slicer_dir):
            path.mkdir(exist_ok=True, mode=0o700)

    def targets(self) -> dict[str, Target]:
        if not self.targets_file:
            return {}
        raw = json.loads(self.targets_file.read_text(encoding="utf-8"))
        targets: dict[str, Target] = {}
        for value in raw.get("targets", []):
            target = Target(
                id=str(value["id"]),
                label=str(value["label"]),
                kind=str(value["kind"]),
                url=str(value["url"]),
            )
            if target.kind not in {"novnc", "emulator"}:
                raise ValueError(f"unsupported registered target kind: {target.kind}")
            if normalized_origin(target.url) not in self.allowed_origins:
                raise ValueError(f"target {target.id} is outside PREVIEWER_ALLOWED_ORIGINS")
            targets[target.id] = target
        return targets


def normalized_origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https")
    if parsed.username or parsed.password:
        raise ValueError("URL credentials are not accepted")
    host = parsed.hostname.lower()
    port = parsed.port
    default_port = 80 if parsed.scheme == "http" else 443
    return f"{parsed.scheme}://{host}" + (f":{port}" if port and port != default_port else "")
