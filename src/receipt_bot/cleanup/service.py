from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from receipt_bot.domain.enums import ReceiptJobState, StorageState
from receipt_bot.domain.time import require_aware, utc_now
from receipt_bot.persistence.database import Database
from receipt_bot.persistence.models import (
    AuditEvent,
    ReceiptImage,
    ReceiptJob,
    TelegramGroup,
    TelegramUser,
)
from receipt_bot.storage import ManagedStorage, UnsafeStoragePath

TERMINAL_JOB_STATES = {
    ReceiptJobState.SUCCEEDED.value,
    ReceiptJobState.DUPLICATE_EXACT.value,
    ReceiptJobState.FAILED_FINAL.value,
}


class CleanupConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    receipt_id: int
    storage_key: str
    byte_size: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CleanupPreview:
    group_id: int
    retention_days: int
    cutoff: datetime
    candidates: tuple[CleanupCandidate, ...]
    total_bytes: int
    fingerprint: str

    @property
    def file_count(self) -> int:
        return len(self.candidates)

    def to_state(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "retention_days": self.retention_days,
            "cutoff": self.cutoff.isoformat(),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "fingerprint": self.fingerprint,
        }

    def matches_state(self, state: dict[str, Any]) -> bool:
        try:
            return (
                self.group_id == int(state["group_id"])
                and self.retention_days == int(state["retention_days"])
                and self.file_count == int(state["file_count"])
                and self.total_bytes == int(state["total_bytes"])
                and self.fingerprint == str(state["fingerprint"])
            )
        except (KeyError, TypeError, ValueError):
            return False


@dataclass(frozen=True, slots=True)
class CleanupResult:
    preview_count: int
    deleted_count: int
    deleted_bytes: int
    missing_count: int
    failed_count: int


def _fingerprint(candidates: list[CleanupCandidate]) -> str:
    payload = [
        {
            "id": item.receipt_id,
            "key": item.storage_key,
            "bytes": item.byte_size,
            "created_at": item.created_at.isoformat(),
        }
        for item in candidates
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ReceiptCleanupService:
    def __init__(self, database: Database, storage: ManagedStorage) -> None:
        self.database = database
        self.storage = storage

    def preview(self, group_id: int, *, now: datetime | None = None) -> CleanupPreview:
        now = require_aware(now or utc_now())
        with self.database.session() as session:
            group = session.get(TelegramGroup, group_id)
            if group is None or not group.active:
                raise CleanupConflict("active group does not exist")
            cutoff = now - timedelta(days=group.retention_days)
            receipts = list(
                session.scalars(
                    select(ReceiptImage)
                    .where(
                        ReceiptImage.group_id == group_id,
                        ReceiptImage.storage_state == StorageState.PRESENT.value,
                        ReceiptImage.storage_key.is_not(None),
                        ReceiptImage.created_at <= cutoff,
                    )
                    .order_by(ReceiptImage.id)
                )
            )
            candidates: list[CleanupCandidate] = []
            for receipt in receipts:
                jobs = list(
                    session.scalars(
                        select(ReceiptJob.status).where(
                            ReceiptJob.group_id == group_id,
                            ReceiptJob.receipt_id == receipt.id,
                        )
                    )
                )
                if jobs and any(status not in TERMINAL_JOB_STATES for status in jobs):
                    continue
                candidates.append(
                    CleanupCandidate(
                        receipt_id=receipt.id,
                        storage_key=str(receipt.storage_key),
                        byte_size=receipt.byte_size,
                        created_at=receipt.created_at,
                    )
                )
        return CleanupPreview(
            group_id=group_id,
            retention_days=group.retention_days,
            cutoff=cutoff,
            candidates=tuple(candidates),
            total_bytes=sum(item.byte_size for item in candidates),
            fingerprint=_fingerprint(candidates),
        )

    def refresh(self, state: dict[str, Any], *, now: datetime | None = None) -> CleanupPreview:
        try:
            group_id = int(state["group_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CleanupConflict("cleanup preview is invalid") from exc
        preview = self.preview(group_id, now=now)
        if not preview.matches_state(state):
            raise CleanupConflict("cleanup eligibility changed; create a new preview")
        return preview

    def execute(
        self,
        preview: CleanupPreview,
        *,
        actor_user_id: int,
        now: datetime | None = None,
    ) -> CleanupResult:
        now = require_aware(now or utc_now())
        current = self.preview(preview.group_id, now=now)
        if current.fingerprint != preview.fingerprint or current.retention_days != preview.retention_days:
            raise CleanupConflict("cleanup eligibility changed; create a new preview")
        with self.database.session() as session:
            actor = session.get(TelegramUser, actor_user_id)
            if actor is None:
                raise CleanupConflict("cleanup actor does not exist")

        deleted_count = 0
        deleted_bytes = 0
        missing_count = 0
        failed_count = 0
        for candidate in current.candidates:
            state: StorageState
            error_code: str | None
            try:
                self.storage.delete_regular_file(candidate.storage_key)
            except FileNotFoundError:
                state = StorageState.MISSING
                error_code = "file_missing"
                missing_count += 1
            except UnsafeStoragePath:
                state = StorageState.DELETE_FAILED
                error_code = "unsafe_storage_path"
                failed_count += 1
            except OSError:
                state = StorageState.DELETE_FAILED
                error_code = "filesystem_error"
                failed_count += 1
            else:
                state = StorageState.DELETED
                error_code = None
                deleted_count += 1
                deleted_bytes += candidate.byte_size
            with self.database.session() as session:
                receipt = session.scalar(
                    select(ReceiptImage).where(
                        ReceiptImage.id == candidate.receipt_id,
                        ReceiptImage.group_id == preview.group_id,
                        ReceiptImage.storage_state == StorageState.PRESENT.value,
                    )
                )
                if receipt is None:
                    continue
                receipt.storage_state = state.value
                receipt.cleaned_at = now
                receipt.cleanup_error = error_code
                session.flush()

        result = CleanupResult(
            preview_count=current.file_count,
            deleted_count=deleted_count,
            deleted_bytes=deleted_bytes,
            missing_count=missing_count,
            failed_count=failed_count,
        )
        with self.database.session() as session:
            session.add(
                AuditEvent(
                    group_id=preview.group_id,
                    actor_user_id=actor_user_id,
                    action="receipt_images.cleanup_executed",
                    target_type="telegram_group",
                    target_id=preview.group_id,
                    metadata_json={
                        "retention_days": preview.retention_days,
                        "cutoff": preview.cutoff.isoformat(),
                        "preview_count": result.preview_count,
                        "deleted_count": result.deleted_count,
                        "deleted_bytes": result.deleted_bytes,
                        "missing_count": result.missing_count,
                        "failed_count": result.failed_count,
                    },
                )
            )
        return result
