from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config, normalized_origin
from .database import Database
from .verification import Verifier


ALLOWED_KINDS = {
    "image", "video", "audio", "markdown", "report", "pdf", "log", "subtitle", "file",
    "html", "live_url", "novnc", "model3d", "blend", "apk", "movie_project",
}
FILE_KINDS = ALLOWED_KINDS - {"live_url", "novnc"}
CHECK_ACTIONS = {"route", "selector", "click", "text", "scroll"}
MOVIE_KINDS = {"video", "movie_project"}
# Agents may place these straight into Movies with "to": "movies".
AGENT_MOVIE_KINDS = {"video", "audio", "movie_project"}
NOTE_SCREENSHOT_LIMIT = 1536 * 1024
# Notes work on 3D models too, with the camera view in place of a timestamp.
NOTE_KINDS = MOVIE_KINDS | {"model3d"}
VIEW_MODES = {"textured", "clay", "wireframe"}
# PrusaSlicer reads these straight; a glTF model reaches it as an STL copy the viewer makes.
SLICER_SUFFIXES = {".stl", ".3mf", ".obj"}
MOVIE_MARKER_TYPES = {"cut", "music", "clip", "note"}
MARKER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class StoreError(ValueError):
    pass


class NotFound(StoreError):
    pass


class Conflict(StoreError):
    pass


class CapacityError(StoreError):
    pass


class Store:
    def __init__(self, config: Config):
        self.config = config
        self.config.prepare()
        self.database = Database(config.db_path)
        self.database.initialize()
        self.verifier = Verifier(config)

    def submit(self, declaration: dict, *, actor: str = "agent") -> dict:
        value = _validate_declaration(declaration, self.config)
        item_id = uuid.uuid4().hex
        now = _now()
        staged_path: Path | None = None
        size_bytes = 0
        digest = None
        mime_type = None
        source_label = value["source"]
        if value["kind"] in FILE_KINDS:
            declared_source = Path(value["source"]).expanduser()
            if declared_source.is_symlink():
                raise StoreError("symlink sources are not accepted")
            source = declared_source.resolve(strict=True)
            _validate_source(source, value["kind"])
            size_bytes = _tree_size(source)
            capacity = self.capacity(extra_bytes=size_bytes)
            if capacity["projected_ratio"] >= 0.90:
                raise CapacityError("staging rejected: configured volume would reach 90% capacity")
            item_dir = self.config.staging_dir / item_id
            item_dir.mkdir(mode=0o700)
            if source.is_dir():
                staged_path = item_dir / "source"
                shutil.copytree(source, staged_path, symlinks=False)
            else:
                staged_path = item_dir / _safe_name(source.name)
                shutil.copy2(source, staged_path, follow_symlinks=False)
            digest = _hash_path(staged_path)
            mime_type = "inode/directory" if staged_path.is_dir() else (mimetypes.guess_type(staged_path.name)[0] or "application/octet-stream")
        else:
            capacity = self.capacity()
            if capacity["ratio"] >= 0.90:
                raise CapacityError("submission rejected: configured volume is at 90% capacity")
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO items(
                    id, project_run_id, title, kind, source, expected_result, target_url, checks_json,
                    state, verification_status, staged_path, sha256, size_bytes, mime_type, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', 'pending', ?, ?, ?, ?, ?, ?)""",
                (
                    item_id, value["project_run_id"], value["title"], value["kind"], source_label,
                    value["expected_result"], value.get("target_url"), json.dumps(value["checks"]),
                    str(staged_path) if staged_path else None, digest, size_bytes, mime_type, now, now,
                ),
            )
            self._audit(connection, item_id, "submitted", actor, {
                "kind": value["kind"], "sha256": digest, "size_bytes": size_bytes,
            })
        return self.get(item_id)

    def verify(self, item_id: str, *, actor: str = "verifier") -> dict:
        item = self.get(item_id)
        if item["state"] != "candidate":
            raise Conflict("only candidates can be verified")
        evidence_dir = self.config.evidence_dir / item_id
        result = self.verifier.verify(item, evidence_dir)
        receipt = result.as_dict()
        capacity = self.capacity()
        receipt["capacity"] = capacity
        if capacity["ratio"] >= 0.80:
            receipt["warnings"].append("configured volume is at or above 80% capacity")
        now = _now()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE items SET verification_status = ?, receipt_json = ?, verified_at = ?, updated_at = ? WHERE id = ?",
                (result.status, json.dumps(receipt), now, now, item_id),
            )
            self._audit(connection, item_id, f"verification_{result.status}", actor, receipt)
        return self.get(item_id)

    def approve(self, item_id: str, *, actor: str = "human", by_agent: bool = False) -> dict:
        """Approve a verified candidate. by_agent records an agent's placement into Movies, not a human approval."""
        item = self.get(item_id)
        if item["state"] == "approved" and item["placed_by_agent"] and not by_agent:
            # A human approves an item an agent placed in Movies: it becomes a human approval.
            now = _now()
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE items SET placed_by_agent = 0, approved_at = ?, updated_at = ? WHERE id = ?", (now, now, item_id)
                )
                self._audit(connection, item_id, "approved", actor, {"sha256": item["sha256"]})
            return self.get(item_id)
        if item["state"] != "candidate" or item["verification_status"] != "ready":
            raise Conflict("only verified candidates can be approved")
        approved_path = None
        moved_from: Path | None = None
        moved_to: Path | None = None
        if item["staged_path"]:
            source = Path(item["staged_path"])
            if _hash_path(source) != item["sha256"]:
                raise Conflict("candidate bytes changed after submission")
            moved_from = source.parent
            moved_to = self.config.approved_dir / item_id
            if moved_from.name != item_id or moved_to.exists():
                raise Conflict("candidate storage layout is invalid")
            os.replace(moved_from, moved_to)
            approved_path = moved_to / source.name
            if _hash_path(approved_path) != item["sha256"]:
                os.replace(moved_to, moved_from)
                raise Conflict("approved copy failed integrity verification")
            _make_immutable_permissions(moved_to)
        now = _now()
        try:
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE items SET state = 'approved', approved_path = ?, staged_path = NULL,
                       approved_at = ?, updated_at = ?, placed_by_agent = ? WHERE id = ?""",
                    (str(approved_path) if approved_path else None, now, now, int(by_agent), item_id),
                )
                self._audit(connection, item_id, "placed_in_movies" if by_agent else "approved", actor, {"sha256": item["sha256"]})
        except Exception:
            if moved_from and moved_to and moved_to.exists():
                _make_writable(moved_to)
                os.replace(moved_to, moved_from)
            raise
        return self.get(item_id)

    def hide(self, item_id: str, *, actor: str = "human") -> dict:
        return self._transition(item_id, "approved", "hidden", "hidden", actor)

    def restore(self, item_id: str, *, actor: str = "human") -> dict:
        return self._transition(item_id, "hidden", "approved", "restored", actor)

    def delete(self, item_id: str, *, actor: str = "human") -> dict:
        item = self.get(item_id)
        # Approved items delete straight from Movies, without hiding first.
        if item["state"] == "deleted":
            raise Conflict("item is already deleted")
        for key in ("staged_path", "approved_path"):
            value = item.get(key)
            if not value:
                continue
            path = Path(value)
            root = path.parent if path.parent.name == item_id else path
            if root.exists():
                _make_writable(root)
                shutil.rmtree(root) if root.is_dir() else root.unlink()
        for folder in (self.config.evidence_dir / item_id, self.config.notes_dir / item_id):
            if folder.exists():
                shutil.rmtree(folder)
        (self.config.slicer_dir / f"{item_id}.stl").unlink(missing_ok=True)
        now = _now()
        with self.database.connect() as connection:
            connection.execute("DELETE FROM movie_edits WHERE item_id = ?", (item_id,))
            connection.execute("DELETE FROM movie_notes WHERE item_id = ?", (item_id,))
            connection.execute(
                """UPDATE items SET state = 'deleted', staged_path = NULL, approved_path = NULL,
                   receipt_json = NULL, updated_at = ? WHERE id = ?""",
                (now, item_id),
            )
            self._audit(connection, item_id, "deleted", actor, {
                "sha256": item["sha256"], "bytes_removed": item["size_bytes"],
            })
        return self.get(item_id)

    def check_replaceable(self, item_id: object, project_run_id: str) -> None:
        """An agent may replace only an earlier candidate from its own project run, never approved work."""
        if not isinstance(item_id, str) or not re.fullmatch(r"[0-9a-f]{32}", item_id):
            raise StoreError("replaces must be an item id")
        item = self.get(item_id)
        if item["state"] != "candidate":
            raise Conflict("only candidates can be replaced")
        if item["project_run_id"] != project_run_id:
            raise Conflict("replaces must name a candidate from the same project_run_id")

    def replace(self, item_id: str, new_item: dict, *, actor: str = "agent") -> dict:
        """Delete item_id now that new_item, from the same run, verified ready."""
        self.check_replaceable(item_id, new_item["project_run_id"])
        if new_item["state"] != "candidate" or new_item["verification_status"] != "ready":
            raise Conflict("the replacement is not a verified candidate")
        with self.database.connect() as connection:
            self._audit(connection, item_id, "replaced", actor, {"by": new_item["id"]})
        return self.delete(item_id, actor=actor)

    def movie_edit(self, item_id: str) -> dict:
        item = self.get(item_id)
        if item["kind"] not in MOVIE_KINDS or item["state"] == "deleted":
            raise NotFound("movie edit is unavailable")
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM movie_edits WHERE item_id = ?", (item_id,)).fetchone()
        if not row:
            return {"item_id": item_id, "revision": 0, "document": None, "updated_at": None}
        return {
            "item_id": item_id,
            "revision": row["revision"],
            "document": json.loads(row["document_json"]),
            "updated_at": row["updated_at"],
        }

    def notes(self, item_id: str) -> list[dict]:
        item = self.get(item_id)
        if item["kind"] not in NOTE_KINDS or item["state"] == "deleted":
            raise NotFound("notes are unavailable")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM movie_notes WHERE item_id = ? ORDER BY at_seconds, created_at", (item_id,)
            ).fetchall()
        return [_note_dict(row) for row in rows]

    def add_note(
        self, item_id: str, at_seconds: object, text: object, screenshot: bytes | None, *, view: object = None, actor: str = "human"
    ) -> dict:
        """Save a note at a timestamp, with an optional JPEG frame. It is stored at once; there is no edit revision.

        On a 3D model the note also keeps the camera view; at_seconds is the animation time, or 0.
        """
        item = self.get(item_id)
        if item["kind"] == "model3d":
            # A model is reviewed before approval, so its notes start in Review Inbox.
            if item["state"] not in {"candidate", "approved"}:
                raise Conflict("notes need a 3D model in review or approved")
            view = _validate_view(view)
        elif item["kind"] not in MOVIE_KINDS or item["state"] != "approved":
            raise Conflict("notes need an approved video or movie project")
        elif view is not None:
            raise StoreError("view is only for notes on 3D models")
        if not isinstance(at_seconds, (int, float)) or isinstance(at_seconds, bool) or not math.isfinite(at_seconds) or at_seconds < 0:
            raise StoreError("at_seconds must be a number of seconds, 0 or more")
        if not isinstance(text, str) or len(text) > 2000:
            raise StoreError("text must be a string of at most 2000 characters")
        text = text.strip()
        if screenshot is not None:
            if not screenshot.startswith(b"\xff\xd8\xff") or len(screenshot) > NOTE_SCREENSHOT_LIMIT:
                raise StoreError("screenshot must be a JPEG of at most 1.5 MiB")
        elif not text:
            raise StoreError("a note needs text or a screenshot")
        if self.capacity(extra_bytes=len(screenshot or b""))["projected_ratio"] >= 0.90:
            raise CapacityError("note rejected: configured volume would reach 90% capacity")
        note_id = uuid.uuid4().hex
        if screenshot is not None:
            folder = self.config.notes_dir / item_id
            folder.mkdir(mode=0o700, exist_ok=True)
            (folder / f"{note_id}.jpg").write_bytes(screenshot)
        now = _now()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO movie_notes(id, item_id, at_seconds, text, has_screenshot, created_at, view_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (note_id, item_id, float(at_seconds), text, int(screenshot is not None), now,
                 json.dumps(view, separators=(",", ":")) if view is not None else None),
            )
            self._audit(connection, item_id, "note_added", actor, {
                "note_id": note_id, "at_seconds": float(at_seconds), "has_screenshot": screenshot is not None,
            })
            row = connection.execute("SELECT * FROM movie_notes WHERE id = ?", (note_id,)).fetchone()
        return _note_dict(row)

    def edit_note(self, item_id: str, note_id: str, text: object, *, actor: str = "human") -> dict:
        """Change a note's text. Its time and screenshot stay as they were."""
        if not isinstance(text, str) or len(text) > 2000:
            raise StoreError("text must be a string of at most 2000 characters")
        text = text.strip()
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM movie_notes WHERE id = ? AND item_id = ?", (note_id, item_id)).fetchone()
            if not row:
                raise NotFound("note not found")
            if not text and not row["has_screenshot"]:
                raise StoreError("a note needs text or a screenshot")
            connection.execute("UPDATE movie_notes SET text = ? WHERE id = ?", (text, note_id))
            self._audit(connection, item_id, "note_edited", actor, {"note_id": note_id, "at_seconds": row["at_seconds"]})
            row = connection.execute("SELECT * FROM movie_notes WHERE id = ?", (note_id,)).fetchone()
        return _note_dict(row)

    def delete_note(self, item_id: str, note_id: str, *, actor: str = "human") -> None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM movie_notes WHERE id = ? AND item_id = ?", (note_id, item_id)).fetchone()
            if not row:
                raise NotFound("note not found")
            connection.execute("DELETE FROM movie_notes WHERE id = ?", (note_id,))
            self._audit(connection, item_id, "note_deleted", actor, {"note_id": note_id, "at_seconds": row["at_seconds"]})
        (self.config.notes_dir / item_id / f"{note_id}.jpg").unlink(missing_ok=True)

    def note_screenshot_path(self, item_id: str, note_id: str) -> Path:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT has_screenshot FROM movie_notes WHERE id = ? AND item_id = ?", (note_id, item_id)
            ).fetchone()
        if not row or not row["has_screenshot"]:
            raise NotFound("note screenshot not found")
        return self.config.notes_dir / item_id / f"{note_id}.jpg"

    def save_movie_edit(self, item_id: str, document: object, base_revision: object, *, actor: str = "human") -> dict:
        item = self.get(item_id)
        if item["kind"] not in MOVIE_KINDS or item["state"] != "approved":
            raise Conflict("movie edits require an approved video or movie project")
        if not isinstance(base_revision, int) or isinstance(base_revision, bool) or base_revision < 0:
            raise StoreError("base_revision must be a non-negative integer")
        clean = _validate_movie_edit(document)
        asset_ids = {marker["asset_item_id"] for marker in clean["annotations"] if marker.get("asset_item_id")}
        if asset_ids:
            placeholders = ",".join("?" for _ in asset_ids)
            with self.database.connect() as connection:
                rows = connection.execute(
                    f"SELECT id, kind, state FROM items WHERE id IN ({placeholders})", tuple(asset_ids)
                ).fetchall()
            assets = {row["id"]: dict(row) for row in rows}
            for marker in clean["annotations"]:
                asset_id = marker.get("asset_item_id")
                if not asset_id:
                    continue
                asset = assets.get(asset_id)
                if not asset or asset["state"] != "approved":
                    raise StoreError("movie edit assets must reference approved items")
                expected_kind = "audio" if marker["type"] == "music" else "video"
                if marker["type"] in {"music", "clip"} and asset["kind"] != expected_kind:
                    raise StoreError(f"{marker['type']} markers must reference approved {expected_kind} items")
        now = _now()
        with self.database.connect() as connection:
            row = connection.execute("SELECT revision FROM movie_edits WHERE item_id = ?", (item_id,)).fetchone()
            current = row["revision"] if row else 0
            if current != base_revision:
                raise Conflict("movie edit changed since it was opened; reload before saving")
            revision = current + 1
            connection.execute(
                """INSERT INTO movie_edits(item_id, revision, document_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(item_id) DO UPDATE SET
                     revision = excluded.revision,
                     document_json = excluded.document_json,
                     updated_at = excluded.updated_at""",
                (item_id, revision, json.dumps(clean, separators=(",", ":"), ensure_ascii=False), now),
            )
            self._audit(connection, item_id, "movie_edit_saved", actor, {
                "revision": revision,
                "annotation_count": len(clean["annotations"]),
            })
        return {"item_id": item_id, "revision": revision, "document": clean, "updated_at": now}

    def get(self, item_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise NotFound("item not found")
        return _item_dict(row)

    def folder_target(self, item_id: str) -> tuple[Path, str]:
        """Return the file to show in a file manager: the submitted original if it still exists, else Previewer's copy."""
        item = self.get(item_id)
        if item["kind"] not in FILE_KINDS or item["state"] == "deleted":
            raise Conflict("only stored files can be shown in a folder")
        original = Path(item["source"])
        if original.is_absolute() and original.exists():
            return original, "original"
        copy = item.get("approved_path") or item.get("staged_path")
        if copy and Path(copy).exists():
            return Path(copy), "copy"
        raise NotFound("neither the original nor Previewer's copy exists")

    def slicer_target(self, item_id: str) -> tuple[Path, str]:
        """Return the STL, 3MF or OBJ file to open in the slicer, preferring the submitted original."""
        item = self.get(item_id)
        if item["kind"] != "model3d" or item["state"] not in {"candidate", "approved"}:
            raise Conflict("only 3D models in review or approved open in the slicer")
        path, location = self.folder_target(item_id)
        if not path.is_file() or path.suffix.lower() not in SLICER_SUFFIXES:
            raise Conflict("the slicer needs an STL copy of this model; send one from the viewer")
        return path, location

    def save_slicer_copy(self, item_id: str, data: bytes) -> Path:
        """Store the binary STL the viewer made from a glTF model, replacing any earlier copy."""
        item = self.get(item_id)
        if item["kind"] != "model3d" or item["state"] not in {"candidate", "approved"}:
            raise Conflict("only 3D models in review or approved open in the slicer")
        triangles = int.from_bytes(data[80:84], "little") if len(data) >= 84 else 0
        if not triangles or len(data) != 84 + 50 * triangles:
            raise StoreError("slicer copy must be a binary STL with at least one triangle")
        target = self.config.slicer_dir / f"{item_id}.stl"
        previous = target.stat().st_size if target.exists() else 0
        if self.capacity(extra_bytes=len(data) - previous)["projected_ratio"] >= 0.90:
            raise CapacityError("slicer copy rejected: configured volume would reach 90% capacity")
        target.write_bytes(data)
        return target

    def list(self, *, state: str | None = None, include_deleted: bool = False) -> list[dict]:
        clauses = []
        values: list[str] = []
        if state:
            clauses.append("state = ?")
            values.append(state)
        elif not include_deleted:
            clauses.append("state != 'deleted'")
        query = "SELECT * FROM items" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at DESC"
        with self.database.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [_item_dict(row) for row in rows]

    def audit(self, item_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM audit_events"
        values: tuple[str, ...] = ()
        if item_id:
            query += " WHERE item_id = ?"
            values = (item_id,)
        query += " ORDER BY sequence ASC"
        with self.database.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]

    def content_path(self, item_id: str, relative: str = "") -> Path:
        item = self.get(item_id)
        if item["state"] == "candidate":
            staged = item.get("staged_path")
            if not staged:
                raise NotFound("candidate content is unavailable")
            root = Path(staged)
            if _hash_path(root) != item["sha256"]:
                raise Conflict("candidate bytes failed integrity verification")
            if relative:
                raise NotFound("content path not found")
            if not root.is_file():
                raise NotFound("candidate content is unavailable")
            return root
        if item["state"] not in {"approved", "hidden"} or not item["approved_path"]:
            raise NotFound("approved content is unavailable")
        root = Path(item["approved_path"])
        if _hash_path(root) != item["sha256"]:
            raise Conflict("approved bytes failed integrity verification")
        if root.is_file():
            if relative:
                raise NotFound("content path not found")
            return root
        if not relative:
            entry = root / "index.html"
            if not entry.is_file():
                model_entries = list(root.glob("*.gltf")) + list(root.glob("*.glb")) + list(root.glob("*.obj"))
                if len(model_entries) != 1:
                    raise NotFound("content entry point not found")
                entry = model_entries[0]
            return entry
        requested = (root / relative).resolve()
        if not requested.is_relative_to(root.resolve()) or not requested.is_file():
            raise NotFound("content path not found")
        return requested

    def evidence_path(self, item_id: str, name: str) -> Path:
        root = (self.config.evidence_dir / item_id).resolve()
        requested = (root / name).resolve()
        if not requested.is_relative_to(root) or not requested.is_file():
            raise NotFound("evidence not found")
        return requested

    def capacity(self, *, extra_bytes: int = 0) -> dict:
        used = _tree_size(self.config.data_dir) if self.config.data_dir.exists() else 0
        limit = self.config.volume_limit_bytes
        projected = used + extra_bytes
        return {
            "used_bytes": used,
            "limit_bytes": limit,
            "ratio": used / limit if limit else 1.0,
            "projected_ratio": projected / limit if limit else 1.0,
            "warning": used / limit >= 0.80 if limit else True,
            "rejecting": projected / limit >= 0.90 if limit else True,
        }

    def _transition(self, item_id: str, old: str, new: str, event: str, actor: str) -> dict:
        item = self.get(item_id)
        if item["state"] != old:
            raise Conflict(f"item must be {old} before it can be {new}")
        now = _now()
        with self.database.connect() as connection:
            connection.execute("UPDATE items SET state = ?, updated_at = ? WHERE id = ?", (new, now, item_id))
            self._audit(connection, item_id, event, actor, {})
        return self.get(item_id)

    @staticmethod
    def _audit(connection: sqlite3.Connection, item_id: str, event: str, actor: str, details: dict) -> None:
        connection.execute(
            "INSERT INTO audit_events(item_id, event, actor, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (item_id, event, actor, json.dumps(details, sort_keys=True), _now()),
        )


def _validate_declaration(declaration: dict, config: Config) -> dict:
    if not isinstance(declaration, dict):
        raise StoreError("declaration must be a JSON object")
    required = ("project_run_id", "title", "kind", "source", "expected_result")
    value: dict = {}
    for key in required:
        item = declaration.get(key)
        if not isinstance(item, str) or not item.strip():
            raise StoreError(f"{key} is required")
        value[key] = item.strip()
    if value["kind"] not in ALLOWED_KINDS:
        raise StoreError(f"unsupported kind: {value['kind']}")
    for key in ("project_run_id", "title"):
        if len(value[key]) > 200:
            raise StoreError(f"{key} is too long")
    if len(value["expected_result"]) > 2000:
        raise StoreError("expected_result is too long")
    checks = declaration.get("checks", [])
    value["checks"] = _validate_checks(checks)
    target_url = None
    if value["kind"] == "live_url":
        target_url = value["source"]
        if normalized_origin(target_url) not in config.allowed_origins:
            raise StoreError("live URL origin is not allow-listed")
    elif value["kind"] == "novnc" or value["kind"] in {"blend", "apk"}:
        target_id = declaration.get("target_id")
        if value["kind"] in {"blend", "apk"} and not target_id:
            raise StoreError(f"{value['kind']} requires target_id")
        if target_id:
            target = config.targets().get(str(target_id))
            if not target or target.kind != "novnc":
                raise StoreError("target_id is not a registered noVNC target")
            target_url = target.url
        if value["kind"] == "novnc":
            if not target_id:
                target = config.targets().get(value["source"])
                if not target or target.kind != "novnc":
                    raise StoreError("source must name a registered noVNC target")
                target_url = target.url
    elif declaration.get("target_id"):
        raise StoreError("target_id is only valid for noVNC, .blend, or APK submissions")
    value["target_url"] = target_url
    to = declaration.get("to")
    if to is not None:
        if to != "movies":
            raise StoreError('to must be "movies" when set')
        if value["kind"] not in AGENT_MOVIE_KINDS:
            raise StoreError("to movies takes only video, audio or movie_project")
    return value


def _validate_movie_edit(document: object) -> dict:
    if not isinstance(document, dict) or document.get("version") != 1:
        raise StoreError("movie edit must be a version 1 JSON object")
    title = document.get("title")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
        raise StoreError("movie edit title is required and limited to 200 characters")
    duration = _finite_number(document.get("duration_seconds"), "duration_seconds", minimum=0.01, maximum=86_400)
    frame_rate = _finite_number(document.get("frame_rate"), "frame_rate", minimum=1, maximum=240)
    resolution = document.get("resolution")
    if not isinstance(resolution, dict):
        raise StoreError("resolution must contain width and height")
    width = _bounded_int(resolution.get("width"), "resolution width", 1, 16_384)
    height = _bounded_int(resolution.get("height"), "resolution height", 1, 16_384)
    color_space = document.get("color_space", "Unknown")
    if not isinstance(color_space, str) or len(color_space.strip()) > 80:
        raise StoreError("color_space must be at most 80 characters")
    annotations = document.get("annotations", [])
    if not isinstance(annotations, list) or len(annotations) > 500:
        raise StoreError("annotations must be a list with at most 500 entries")
    clean_annotations = []
    seen_ids: set[str] = set()
    for raw in annotations:
        if not isinstance(raw, dict):
            raise StoreError("each movie annotation must be an object")
        marker_id = raw.get("id")
        if not isinstance(marker_id, str) or not MARKER_ID_RE.fullmatch(marker_id) or marker_id in seen_ids:
            raise StoreError("movie annotation IDs must be unique safe strings")
        seen_ids.add(marker_id)
        marker_type = raw.get("type")
        if marker_type not in MOVIE_MARKER_TYPES:
            raise StoreError("movie annotation type must be cut, music, clip, or note")
        start = _finite_number(raw.get("start_seconds"), "annotation start", minimum=0, maximum=duration)
        end = raw.get("end_seconds", start)
        end = _finite_number(end, "annotation end", minimum=start, maximum=duration)
        if marker_type in {"music", "clip"} and end <= start:
            raise StoreError(f"{marker_type} annotations require an end after the start")
        label = raw.get("label")
        notes = raw.get("notes", "")
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 160:
            raise StoreError("annotation labels are required and limited to 160 characters")
        if not isinstance(notes, str) or len(notes) > 2_000:
            raise StoreError("annotation notes are limited to 2000 characters")
        clean = {
            "id": marker_id,
            "type": marker_type,
            "start_seconds": start,
            "end_seconds": end,
            "label": label.strip(),
            "notes": notes.strip(),
        }
        asset_id = raw.get("asset_item_id")
        if asset_id is not None:
            if not isinstance(asset_id, str) or not re.fullmatch(r"[0-9a-f]{32}", asset_id):
                raise StoreError("asset_item_id must be a Previewer item ID")
            clean["asset_item_id"] = asset_id
        clean_annotations.append(clean)
    return {
        "version": 1,
        "title": title.strip(),
        "duration_seconds": duration,
        "frame_rate": frame_rate,
        "resolution": {"width": width, "height": height},
        "color_space": color_space.strip() or "Unknown",
        "annotations": clean_annotations,
    }


def _finite_number(value: object, name: str, *, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise StoreError(f"{name} must be a number")
    number = float(value)
    if not minimum <= number <= maximum or number != number or number in {float("inf"), float("-inf")}:
        raise StoreError(f"{name} must be between {minimum} and {maximum}")
    return round(number, 6)


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise StoreError(f"{name} must be between {minimum} and {maximum}")
    return value


def _validate_checks(checks: object) -> list[dict]:
    if not isinstance(checks, list) or len(checks) > 20:
        raise StoreError("checks must be a list with at most 20 entries")
    validated = []
    for raw in checks:
        if not isinstance(raw, dict) or raw.get("action") not in CHECK_ACTIONS:
            raise StoreError("checks contain an unsupported action")
        action = raw["action"]
        if action == "route":
            path = raw.get("path")
            if not isinstance(path, str) or not path.startswith("/") or "://" in path:
                raise StoreError("route check requires a local absolute path")
            check = {"action": action, "path": path}
        elif action in {"selector", "click"}:
            selector = _selector(raw.get("selector"))
            check = {"action": action, "selector": selector}
        elif action == "text":
            selector = _selector(raw.get("selector"))
            contains = raw.get("contains")
            if not isinstance(contains, str) or not contains or len(contains) > 500:
                raise StoreError("text check requires contains")
            check = {"action": action, "selector": selector, "contains": contains}
        else:
            pixels = raw.get("pixels")
            if not isinstance(pixels, int) or not 1 <= pixels <= 20_000:
                raise StoreError("scroll pixels must be between 1 and 20000")
            check = {"action": action, "pixels": pixels}
        validated.append(check)
    return validated


def _selector(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise StoreError("selector is required and must be at most 500 characters")
    lowered = value.lower()
    if any(token in lowered for token in ("javascript:", "<script", "${", "`")):
        raise StoreError("selector contains executable content")
    return value


def _validate_source(source: Path, kind: str) -> None:
    if source.is_symlink():
        raise StoreError("symlink sources are not accepted")
    if source.is_dir():
        if kind not in {"html", "model3d"}:
            raise StoreError("directories are accepted only for static HTML and GLTF submissions")
        if kind == "html" and not (source / "index.html").is_file():
            raise StoreError("HTML directory must contain index.html")
        if kind == "model3d" and len(list(source.glob("*.gltf"))) != 1:
            raise StoreError("3D directory must contain exactly one top-level GLTF file")
        for child in source.rglob("*"):
            if child.is_symlink():
                raise StoreError("HTML directories may not contain symlinks")
            if not child.is_dir() and not child.is_file():
                raise StoreError("source directories may contain only regular files")
    elif not source.is_file():
        raise StoreError("source must be a regular file")


def _safe_name(name: str) -> str:
    return "".join(character for character in name if character.isalnum() or character in "._- ").strip() or "artifact"


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    for child in sorted(value for value in path.rglob("*") if value.is_file()):
        digest.update(child.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        with child.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    if path.exists():
        for child in path.rglob("*"):
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
    return total


def _make_immutable_permissions(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(0o555 if path.is_dir() else 0o444)


def _make_writable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(0o700 if path.is_dir() else 0o600)


def _validate_view(view: object) -> dict:
    if not isinstance(view, dict):
        raise StoreError("a note on a 3D model needs its view")
    clean = {}
    for key in ("position", "target"):
        value = view.get(key)
        if not isinstance(value, list) or len(value) != 3:
            raise StoreError(f"view {key} must be three numbers")
        clean[key] = [_finite_number(number, f"view {key}", minimum=-1e9, maximum=1e9) for number in value]
    if view.get("mode") not in VIEW_MODES:
        raise StoreError("view mode must be textured, clay or wireframe")
    clean["mode"] = view["mode"]
    if "clip" in view:
        if not isinstance(view["clip"], str) or len(view["clip"]) > 200:
            raise StoreError("view clip must be an animation name of at most 200 characters")
        clean["clip"] = view["clip"]
    return clean


def _note_dict(row: sqlite3.Row) -> dict:
    note = dict(row)
    note["has_screenshot"] = bool(note["has_screenshot"])
    view_json = note.pop("view_json", None)
    note["view"] = json.loads(view_json) if view_json else None
    return note


def _item_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["checks"] = json.loads(item.pop("checks_json"))
    receipt_json = item.pop("receipt_json")
    item["receipt"] = json.loads(receipt_json) if receipt_json else None
    item["placed_by_agent"] = bool(item["placed_by_agent"])
    return item


def _now() -> str:
    return datetime.now(UTC).isoformat()
