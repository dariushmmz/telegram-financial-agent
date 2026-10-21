from __future__ import annotations

import asyncio
from datetime import timedelta

from PIL import Image, ImageOps
from sqlalchemy import select

from receipt_bot.domain.enums import ReceiptJobState, StorageState
from receipt_bot.domain.time import utc_now
from receipt_bot.extraction.errors import ExtractionError
from receipt_bot.extraction.image import ImageLimits, prepare_image
from receipt_bot.extraction.service import ExtractionService
from receipt_bot.persistence.database import Database
from receipt_bot.persistence.models import (
    PaymentEvidence,
    ReceiptImage,
    TelegramGroup,
    TelegramMessage,
)
from receipt_bot.persistence.repositories import (
    ExtractionAttemptRepository,
    ReceiptJobRepository,
    TextReceiptJobRepository,
)
from receipt_bot.reconciliation.service import LedgerService
from receipt_bot.storage import ManagedStorage
from receipt_bot.telegram.access import TelegramAccessPolicy
from receipt_bot.telegram.api import TelegramAPIError
from receipt_bot.telegram.contracts import TelegramGateway


def perceptual_hash(data: bytes) -> str:
    """Return a conservative 64-bit dHash used only to suggest possible duplicates."""
    from io import BytesIO

    with Image.open(BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(image.getdata())
    value = 0
    for row in range(8):
        for column in range(8):
            value = (value << 1) | int(
                pixels[row * 9 + column] > pixels[row * 9 + column + 1]
            )
    return f"{value:016x}"


def _provider_identity(service: ExtractionService) -> tuple[str, str]:
    provider = service.provider
    name = str(getattr(provider, "name", "unknown"))
    models = getattr(provider, "models", None)
    model = str(models[0]) if models else str(getattr(provider, "model", "configured"))
    return name, model


class ReceiptJobWorker:
    """Runs durable download/extraction stages outside the Telegram update handler."""

    def __init__(
        self,
        database: Database,
        gateway: TelegramGateway,
        storage: ManagedStorage,
        extraction_service: ExtractionService,
        *,
        access_policy: TelegramAccessPolicy,
        limits: ImageLimits | None = None,
        retry_delay_seconds: int = 30,
    ) -> None:
        self.database = database
        self.gateway = gateway
        self.storage = storage
        self.extraction_service = extraction_service
        self.access_policy = access_policy
        self.limits = limits or ImageLimits()
        self.retry_delay_seconds = retry_delay_seconds

    def recover_stale(self, *, older_than_minutes: int = 0) -> int:
        with self.database.session() as session:
            before = utc_now() - timedelta(minutes=older_than_minutes)
            return (
                ReceiptJobRepository(session).recover_stale(before=before)
                + TextReceiptJobRepository(session).recover_stale(before=before)
            )

    def materialize_extracted_payments(self) -> int:
        with self.database.session() as session:
            groups = list(session.scalars(select(TelegramGroup).order_by(TelegramGroup.id)))
            return sum(
                LedgerService(session, group.id).materialize_unrecorded_extractions()
                for group in groups
                if self.access_policy.authorizes_group(group)
            )

    async def run_once(self) -> bool:
        with self.database.session() as session:
            job = ReceiptJobRepository(session).claim_next()
            if job is None:
                text_job = TextReceiptJobRepository(session).claim_next()
                if text_job is None:
                    return False
                text_job_id = text_job.id
                text_job_group_id = text_job.group_id
                job_id = None
            else:
                text_job_id = None
                text_job_group_id = None
                job_id = job.id
                job_group_id = job.group_id
                stage = (
                    "download"
                    if job.status == ReceiptJobState.DOWNLOADING.value
                    else "extraction"
                )
        if text_job_id is not None:
            if not self._group_is_authorized(text_job_group_id):
                self._fail_text_job(
                    text_job_id,
                    error_code="group_unauthorized",
                    retryable=False,
                )
                return True
            await self._extract_text(text_job_id)
            return True
        if not self._group_is_authorized(job_group_id):
            self._fail_job(
                job_id,
                stage=stage,
                error_code="group_unauthorized",
                retryable=False,
            )
            return True
        if stage == "download":
            await self._download(job_id)
        else:
            await self._extract(job_id)
        return True

    def _group_is_authorized(self, group_id: int) -> bool:
        with self.database.session() as session:
            return self.access_policy.authorizes_group(session.get(TelegramGroup, group_id))

    async def _download(self, job_id: int) -> None:
        with self.database.session() as session:
            repository = ReceiptJobRepository(session)
            job = repository.get(job_id)
            existing = repository.find_by_file_unique_id(
                job.group_id, job.telegram_file_unique_id
            )
            if existing is not None:
                job.receipt_id = existing.id
                canonical = repository.ensure_evidence_for_receipt(existing)
                repository.create_image_evidence(
                    job=job,
                    receipt=existing,
                    duplicate_of_evidence_id=canonical.id,
                    duplicate_disposition="exact",
                )
                repository.complete(job, duplicate=True)
                return
            file_id = job.telegram_file_id
            declared_mime_type = job.declared_mime_type

        try:
            data = await self.gateway.download_file(file_id)
            prepared = prepare_image(
                data,
                declared_mime_type=declared_mime_type,
                limits=self.limits,
            )
            visual_hash = perceptual_hash(data)
        except TelegramAPIError as exc:
            self._fail_job(
                job_id,
                stage="download",
                error_code=exc.code,
                retryable=exc.retryable,
            )
            return
        except ExtractionError as exc:
            self._fail_job(job_id, stage="download", error_code=exc.code, retryable=False)
            return
        except (OSError, RuntimeError):
            self._fail_job(job_id, stage="download", error_code="storage_io", retryable=True)
            return

        with self.database.session() as session:
            repository = ReceiptJobRepository(session)
            job = repository.get(job_id)
            exact = repository.find_exact(job.group_id, prepared.original_sha256)
            if exact is not None:
                duplicate_receipt = repository.create_receipt(
                    job=job,
                    mime_type=prepared.mime_type,
                    byte_size=len(data),
                    sha256=prepared.original_sha256,
                    perceptual_hash=visual_hash,
                    storage_key=None,
                    storage_state=StorageState.DEDUPLICATED.value,
                    duplicate_of_receipt_id=exact.id,
                    duplicate_disposition="exact",
                )
                canonical = repository.ensure_evidence_for_receipt(exact)
                repository.create_image_evidence(
                    job=job,
                    receipt=duplicate_receipt,
                    duplicate_of_evidence_id=canonical.id,
                    duplicate_disposition="exact",
                )
                repository.complete(job, duplicate=True)
                return
            candidate = repository.find_candidate(job.group_id, visual_hash)
            storage_key = (
                f"group-{job.group_id}/{prepared.original_sha256[:2]}/"
                f"{prepared.original_sha256}.{prepared.extension}"
            )

        try:
            self.storage.write_bytes_atomic(storage_key, data)
        except (OSError, ValueError):
            self._fail_job(job_id, stage="download", error_code="storage_io", retryable=True)
            return

        with self.database.session() as session:
            repository = ReceiptJobRepository(session)
            job = repository.get(job_id)
            receipt = repository.create_receipt(
                job=job,
                mime_type=prepared.mime_type,
                byte_size=len(data),
                sha256=prepared.original_sha256,
                perceptual_hash=visual_hash,
                storage_key=storage_key,
                duplicate_of_receipt_id=candidate.id if candidate else None,
                duplicate_disposition="candidate" if candidate else None,
            )
            candidate_evidence = (
                repository.ensure_evidence_for_receipt(candidate) if candidate else None
            )
            repository.create_image_evidence(
                job=job,
                receipt=receipt,
                duplicate_of_evidence_id=candidate_evidence.id if candidate_evidence else None,
                duplicate_disposition="candidate" if candidate_evidence else None,
            )
            repository.queue_extraction(job)

    async def _extract(self, job_id: int) -> None:
        missing_error: str | None = None
        with self.database.session() as session:
            job = ReceiptJobRepository(session).get(job_id)
            if job.receipt_id is None:
                missing_error = "receipt_missing"
            elif job.evidence_id is None:
                missing_error = "evidence_missing"
            else:
                receipt = session.get(ReceiptImage, job.receipt_id)
                if receipt is None or not receipt.storage_key:
                    missing_error = "receipt_file_missing"
                else:
                    group_id = job.group_id
                    evidence_id = job.evidence_id
                    storage_key = receipt.storage_key
                    mime_type = receipt.mime_type
        if missing_error is not None:
            self._fail_job(
                job_id,
                stage="extraction",
                error_code=missing_error,
                retryable=False,
            )
            return

        try:
            data = self.storage.read_bytes(storage_key)
            outcome = await asyncio.to_thread(
                self.extraction_service.extract,
                data,
                declared_mime_type=mime_type,
            )
        except ExtractionError as exc:
            provider, model = _provider_identity(self.extraction_service)
            with self.database.session() as session:
                ExtractionAttemptRepository(session, group_id).append_failure(
                    evidence_id=evidence_id,
                    provider=provider,
                    model=model,
                    error_code=exc.code,
                    retryable=exc.retryable,
                )
            self._fail_job(
                job_id,
                stage="extraction",
                error_code=exc.code,
                retryable=exc.retryable,
            )
            return
        except (OSError, ValueError):
            self._fail_job(
                job_id,
                stage="extraction",
                error_code="receipt_file_unavailable",
                retryable=True,
            )
            return

        with self.database.session() as session:
            ExtractionAttemptRepository(session, group_id).append_success(
                evidence_id=evidence_id, outcome=outcome
            )
            LedgerService(session, group_id).create_payment_from_evidence(evidence_id)
            job = ReceiptJobRepository(session, group_id).get(job_id)
            ReceiptJobRepository(session, group_id).complete(job)

    async def _extract_text(self, job_id: int) -> None:
        with self.database.session() as session:
            job = TextReceiptJobRepository(session).get(job_id)
            evidence = session.get(PaymentEvidence, job.evidence_id)
            message = session.get(TelegramMessage, job.source_message_id)
            if (
                evidence is None
                or evidence.group_id != job.group_id
                or message is None
                or message.group_id != job.group_id
                or not message.text
            ):
                missing = True
            else:
                missing = False
                group_id = job.group_id
                evidence_id = evidence.id
                receipt_text = message.text
        if missing:
            self._fail_text_job(
                job_id, error_code="receipt_text_missing", retryable=False
            )
            return

        try:
            outcome = await asyncio.to_thread(
                self.extraction_service.extract_text, receipt_text
            )
        except ExtractionError as exc:
            provider, model = _provider_identity(self.extraction_service)
            with self.database.session() as session:
                ExtractionAttemptRepository(session, group_id).append_failure(
                    evidence_id=evidence_id,
                    provider=provider,
                    model=model,
                    error_code=exc.code,
                    retryable=exc.retryable,
                )
            self._fail_text_job(
                job_id, error_code=exc.code, retryable=exc.retryable
            )
            return
        except ValueError:
            self._fail_text_job(
                job_id, error_code="receipt_text_invalid", retryable=False
            )
            return

        with self.database.session() as session:
            ExtractionAttemptRepository(session, group_id).append_success(
                evidence_id=evidence_id, outcome=outcome
            )
            LedgerService(session, group_id).create_payment_from_evidence(evidence_id)
            job = TextReceiptJobRepository(session, group_id).get(job_id)
            TextReceiptJobRepository(session, group_id).complete(job)

    def _fail_job(
        self, job_id: int, *, stage: str, error_code: str, retryable: bool
    ) -> None:
        with self.database.session() as session:
            repository = ReceiptJobRepository(session)
            job = repository.get(job_id)
            repository.fail(
                job,
                stage=stage,
                error_code=error_code,
                retryable=retryable,
                retry_delay_seconds=self.retry_delay_seconds,
            )

    def _fail_text_job(
        self, job_id: int, *, error_code: str, retryable: bool
    ) -> None:
        with self.database.session() as session:
            repository = TextReceiptJobRepository(session)
            job = repository.get(job_id)
            repository.fail(
                job,
                error_code=error_code,
                retryable=retryable,
                retry_delay_seconds=self.retry_delay_seconds,
            )
