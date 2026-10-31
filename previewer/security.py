from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime

from .database import Database


class AuthError(PermissionError):
    pass


class RateLimited(AuthError):
    pass


def hash_pin(pin: str, *, salt: bytes | None = None) -> str:
    if not isinstance(pin, str) or not pin.isdigit() or not 4 <= len(pin) <= 32:
        raise ValueError("PIN must contain 4 to 32 digits")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            pin.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


class PinGuard:
    def __init__(
        self,
        database: Database,
        encoded_pin: str,
        *,
        max_attempts: int = 5,
        window_seconds: int = 300,
    ):
        self.database = database
        self.encoded_pin = encoded_pin
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds

    def configured(self) -> bool:
        with self.database.connect() as connection:
            return bool(self._encoded_pin(connection))

    def require(self, pin: str, client_key: str) -> None:
        now = int(time.time())
        cutoff = now - self.window_seconds
        outcome = "ok"
        with self.database.connect() as connection:
            encoded_pin = self._encoded_pin(connection)
            if not encoded_pin:
                raise AuthError("human actions are disabled until PREVIEWER_PIN_HASH is configured")
            connection.execute("DELETE FROM auth_attempts WHERE attempted_at < ?", (cutoff,))
            attempts = connection.execute(
                "SELECT COUNT(*) FROM auth_attempts WHERE key = ?", (client_key,)
            ).fetchone()[0]
            if attempts >= self.max_attempts:
                outcome = "limited"
            elif not verify_pin(pin, encoded_pin):
                connection.execute(
                    "INSERT INTO auth_attempts(key, attempted_at) VALUES (?, ?)",
                    (client_key, now),
                )
                outcome = "invalid"
            else:
                connection.execute("DELETE FROM auth_attempts WHERE key = ?", (client_key,))
        if outcome == "limited":
            raise RateLimited("too many failed PIN attempts")
        if outcome == "invalid":
            raise AuthError("invalid PIN")

    def change(self, current_pin: str, new_pin: str, client_key: str) -> None:
        encoded = hash_pin(new_pin)
        self.require(current_pin, client_key)
        now = datetime.now(UTC).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO settings(key, value, updated_at) VALUES ('pin_hash', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (encoded, now),
            )
            connection.execute(
                "INSERT INTO audit_events(item_id, event, actor, details_json, created_at) VALUES ('system', 'pin_changed', 'human', '{}', ?)",
                (now,),
            )

    def _encoded_pin(self, connection) -> str:
        row = connection.execute("SELECT value FROM settings WHERE key = 'pin_hash'").fetchone()
        return row["value"] if row else self.encoded_pin
