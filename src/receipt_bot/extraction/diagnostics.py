from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from receipt_bot.extraction.image import PreparedImage
from receipt_bot.storage import ManagedStorage


class DiagnosticCapture:
    """Opt-in capture of provider-bound bytes under a managed storage root."""

    def __init__(self, root: Path, *, enabled: bool = False) -> None:
        self.storage = ManagedStorage(root)
        self.enabled = enabled

    def capture_prepared(self, group_id: int, image: PreparedImage) -> dict[str, str] | None:
        if not self.enabled:
            return None
        self.storage.ensure_exists()
        capture_id = uuid4().hex
        prefix = f"group_{group_id}/{capture_id}"
        image_key = f"{prefix}.{image.extension}"
        metadata_key = f"{prefix}.json"
        image_path = self.storage.resolve_key(image_key)
        metadata_path = self.storage.resolve_key(metadata_key)
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(image.data)
        metadata_path.write_text(
            json.dumps(image.record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"image_key": image_key, "metadata_key": metadata_key}
