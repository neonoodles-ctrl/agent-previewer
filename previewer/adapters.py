"""Previewer item URL helper used by `previewer open`."""

from __future__ import annotations

import re


PREVIEWER_ITEM_RE = re.compile(r"^[0-9a-f]{32}$")


class AdapterError(ValueError):
    pass


def previewer_item_url(base_url: str, item_id: str | None = None) -> str:
    base = base_url.rstrip("/")
    if item_id:
        if not PREVIEWER_ITEM_RE.fullmatch(item_id):
            raise AdapterError("Previewer item ID is invalid")
        return f"{base}/?item={item_id}"
    return f"{base}/"
