from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from time import monotonic, perf_counter

from PIL import Image, ImageOps
from sqlalchemy import select

from receipt_bot.domain.enums import MembershipRole, ReceiptJobState, StorageState
from receipt_bot.domain.time import utc_now
from receipt_bot.extraction.errors import ExtractionError
from receipt_bot.extraction.image import ImageLimits, prepare_image
from receipt_bot.extraction.service import ExtractionService
from receipt_bot.persistence.database import Database
from receipt_bot.persistence.models import (
    GroupMembership,
    PaymentEvidence,
    ReceiptImage,
    ReceiptJob,
    TelegramGroup,
    TelegramMessage,
    TelegramUser,
    TextReceiptJob,
)
from receipt_bot.persistence.repositories import (
    ExtractionAttemptRepository,
    ReceiptJobRepository,
    TextReceiptJobRepository,
)
from receipt_bot.reconciliation.service import LedgerService
from receipt_bot.storage import ManagedStorage
from receipt_bot.telegram import texts
from receipt_bot.telegram.access import TelegramAccessPolicy
from receipt_bot.telegram.api import TelegramAPIError
from receipt_bot.telegram.contracts import TelegramGateway

LOGGER = logging.getLogger(__name__)


class ProviderCooldown:
    """Small process-local circuit breaker; durable job limits remain the safety net."""

    def __init__(
        self,
        *,
        base_seconds: float,
        max_seconds: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.base_seconds = base_seconds
        self.max_seconds = max(max_seconds, base_seconds)
        self.clock = clock
        self._until = 0.0
        self._consecutive_rate_limits = 0

    @property
    def is_open(self) -> bool:
        return self.clock() < self._until

    @property
    def remaining_seconds(self) -> float:
        return max(self._until - self.clock(), 0.0)

    def record_rate_limit(self, retry_after_seconds: float | None = None) -> float:
        self._consecutive_rate_limits += 1
        circuit_delay = min(
            self.base_seconds * (2 ** (self._consecutive_rate_limits - 1)),
            self.max_seconds,
        )
        delay = max(circuit_delay, retry_after_seconds or 0.0)
        self._until = max(self._until, self.clock() + delay)
        return delay

    def record_success(self) -> None:
        self._consecutive_rate_limits = 0
        self._until = 0.0


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
        retry_delay_seconds: float | None = None,
        retry_base_seconds: float = 30,
        retry_max_seconds: float = 900,
        retry_jitter_ratio: float = 0.20,
        max_download_attempts: int = 3,
        max_extraction_attempts: int = 5,
        provider_cooldown_seconds: float = 60,
        delay_notification_seconds: int = 300,
        random_source: Callable[[], float] = random.random,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.database = database
        self.gateway = gateway
        self.storage = storage
        self.extraction_service = extraction_service
        self.access_policy = access_policy
        self.limits = limits or ImageLimits()
        self.retry_base_seconds = (
            float(retry_delay_seconds)
            if retry_delay_seconds is not None
            else float(retry_base_seconds)
        )
        self.retry_max_seconds = max(float(retry_max_seconds), self.retry_base_seconds)
        self.retry_jitter_ratio = retry_jitter_ratio
        self.max_download_attempts = max_download_attempts
        self.max_extraction_attempts = max_extraction_attempts
        self.delay_notification_seconds = delay_notification_seconds
        self.random_source = random_source
        self._prefer_text = False
        self.provider_cooldown = ProviderCooldown(
            base_seconds=provider_cooldown_seconds,
            max_seconds=self.retry_max_seconds,
            clock=clock,
        )

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
        notified = await self._notify_one_due_job()
        cooldown_open = self.provider_cooldown.is_open
        with self.database.session() as session:
            image_repository = ReceiptJobRepository(session)
            text_repository = TextReceiptJobRepository(session)
            job = None
            text_job = None
            if cooldown_open:
                # Downloads do not consume provider quota and remain available.
                job = image_repository.claim_next(allow_extraction=False)
            elif self._prefer_text:
                text_job = text_repository.claim_next()
                if text_job is None:
                    job = image_repository.claim_next()
            else:
                job = image_repository.claim_next()
                if job is None:
                    text_job = text_repository.claim_next()

            if job is None:
                if text_job is None:
                    return notified
                text_job_id = text_job.id
                text_job_group_id = text_job.group_id
                job_id = None
                self._prefer_text = False
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
                self._prefer_text = True
        if text_job_id is not None:
            if not self._group_is_authorized(text_job_group_id):
                self._fail_text_job(
                    text_job_id,
                    error_code="group_unauthorized",
                    retryable=False,
                )
                await self._notify_one_due_job()
                return True
            if self._attempt_limit_reached(text_job_group_id, text_job_id, text=True):
                self._fail_text_job(
                    text_job_id, error_code="retry_exhausted", retryable=False
                )
                await self._notify_one_due_job()
                return True
            self._log_started("text", text_job_id, text_job_group_id, "extraction")
            await self._extract_text(text_job_id)
            await self._notify_one_due_job()
            return True
        if not self._group_is_authorized(job_group_id):
            self._fail_job(
                job_id,
                stage=stage,
                error_code="group_unauthorized",
                retryable=False,
            )
            await self._notify_one_due_job()
            return True
        if stage == "extraction" and self._attempt_limit_reached(
            job_group_id, job_id, text=False
        ):
            self._fail_job(
                job_id,
                stage=stage,
                error_code="retry_exhausted",
                retryable=False,
            )
            await self._notify_one_due_job()
            return True
        self._log_started("image", job_id, job_group_id, stage)
        if stage == "download":
            await self._download(job_id)
        else:
            await self._extract(job_id)
        await self._notify_one_due_job()
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
                self._log_completed(
                    "image", job.id, job.group_id, "duplicate_exact", job.attempt_count
                )
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
                self._log_completed(
                    "image", job.id, job.group_id, "duplicate_exact", job.attempt_count
                )
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
            LOGGER.info(
                "receipt image download completed",
                extra={
                    "context": {
                        "job_kind": "image",
                        "job_id": job.id,
                        "group_id": job.group_id,
                        "evidence_id": job.evidence_id,
                        "attempt_count": job.attempt_count,
                    }
                },
            )

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

        started = perf_counter()
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
                attempt = ExtractionAttemptRepository(session, group_id).append_failure(
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
                retry_after_seconds=getattr(exc, "retry_after_seconds", None),
                extraction_attempt_number=attempt.attempt_number,
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
            attempt = ExtractionAttemptRepository(session, group_id).append_success(
                evidence_id=evidence_id, outcome=outcome
            )
            LedgerService(session, group_id).create_payment_from_evidence(evidence_id)
            job = ReceiptJobRepository(session, group_id).get(job_id)
            ReceiptJobRepository(session, group_id).complete(job)
            attempt_count = job.attempt_count
        self.provider_cooldown.record_success()
        self._log_completed(
            "image",
            job_id,
            group_id,
            "succeeded",
            attempt_count,
            evidence_id=evidence_id,
            extraction_attempt=attempt.attempt_number,
            duration_ms=int((perf_counter() - started) * 1000),
        )

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

        started = perf_counter()
        try:
            outcome = await asyncio.to_thread(
                self.extraction_service.extract_text, receipt_text
            )
        except ExtractionError as exc:
            provider, model = _provider_identity(self.extraction_service)
            with self.database.session() as session:
                attempt = ExtractionAttemptRepository(session, group_id).append_failure(
                    evidence_id=evidence_id,
                    provider=provider,
                    model=model,
                    error_code=exc.code,
                    retryable=exc.retryable,
                )
            self._fail_text_job(
                job_id,
                error_code=exc.code,
                retryable=exc.retryable,
                retry_after_seconds=getattr(exc, "retry_after_seconds", None),
                extraction_attempt_number=attempt.attempt_number,
            )
            return
        except ValueError:
            self._fail_text_job(
                job_id, error_code="receipt_text_invalid", retryable=False
            )
            return

        with self.database.session() as session:
            attempt = ExtractionAttemptRepository(session, group_id).append_success(
                evidence_id=evidence_id, outcome=outcome
            )
            LedgerService(session, group_id).create_payment_from_evidence(evidence_id)
            job = TextReceiptJobRepository(session, group_id).get(job_id)
            TextReceiptJobRepository(session, group_id).complete(job)
            attempt_count = job.attempt_count
        self.provider_cooldown.record_success()
        self._log_completed(
            "text",
            job_id,
            group_id,
            "succeeded",
            attempt_count,
            evidence_id=evidence_id,
            extraction_attempt=attempt.attempt_number,
            duration_ms=int((perf_counter() - started) * 1000),
        )

    def _fail_job(
        self,
        job_id: int,
        *,
        stage: str,
        error_code: str,
        retryable: bool,
        retry_after_seconds: float | None = None,
        extraction_attempt_number: int | None = None,
    ) -> None:
        with self.database.session() as session:
            repository = ReceiptJobRepository(session)
            job = repository.get(job_id)
            limit = (
                self.max_download_attempts
                if stage == "download"
                else self.max_extraction_attempts
            )
            attempt_number = extraction_attempt_number or job.attempt_count
            can_retry = retryable and attempt_number < limit
            delay = self._retry_delay(attempt_number, retry_after_seconds)
            if error_code == "provider_rate_limit":
                delay = max(
                    delay,
                    self.provider_cooldown.record_rate_limit(retry_after_seconds),
                )
            processing_started_at = job.processing_started_at
            repository.fail(
                job,
                stage=stage,
                error_code=error_code,
                retryable=can_retry,
                retry_delay_seconds=delay,
            )
            self._log_failed(
                "image",
                job,
                error_code,
                can_retry,
                delay,
                extraction_attempt_number,
                processing_started_at,
            )

    def _fail_text_job(
        self,
        job_id: int,
        *,
        error_code: str,
        retryable: bool,
        retry_after_seconds: float | None = None,
        extraction_attempt_number: int | None = None,
    ) -> None:
        with self.database.session() as session:
            repository = TextReceiptJobRepository(session)
            job = repository.get(job_id)
            attempt_number = extraction_attempt_number or job.attempt_count
            can_retry = retryable and attempt_number < self.max_extraction_attempts
            delay = self._retry_delay(attempt_number, retry_after_seconds)
            if error_code == "provider_rate_limit":
                delay = max(
                    delay,
                    self.provider_cooldown.record_rate_limit(retry_after_seconds),
                )
            processing_started_at = job.processing_started_at
            repository.fail(
                job,
                error_code=error_code,
                retryable=can_retry,
                retry_delay_seconds=delay,
            )
            self._log_failed(
                "text",
                job,
                error_code,
                can_retry,
                delay,
                extraction_attempt_number,
                processing_started_at,
            )

    def _attempt_limit_reached(self, group_id: int, job_id: int, *, text: bool) -> bool:
        with self.database.session() as session:
            if text:
                job = TextReceiptJobRepository(session, group_id).get(job_id)
            else:
                job = ReceiptJobRepository(session, group_id).get(job_id)
            evidence_id = job.evidence_id
            if evidence_id is None:
                return False
            count = ExtractionAttemptRepository(session, group_id).count_for_evidence(
                evidence_id
            )
            return count >= self.max_extraction_attempts

    def _retry_delay(
        self, attempt_number: int, retry_after_seconds: float | None = None
    ) -> float:
        exponent = max(attempt_number - 1, 0)
        base = min(self.retry_base_seconds * (2**exponent), self.retry_max_seconds)
        jitter = base * self.retry_jitter_ratio * max(min(self.random_source(), 1.0), 0.0)
        return max(base + jitter, retry_after_seconds or 0.0)

    @staticmethod
    def _log_started(kind: str, job_id: int, group_id: int, stage: str) -> None:
        LOGGER.info(
            "receipt job started",
            extra={
                "context": {
                    "job_kind": kind,
                    "job_id": job_id,
                    "group_id": group_id,
                    "stage": stage,
                }
            },
        )

    @staticmethod
    def _log_completed(
        kind: str,
        job_id: int,
        group_id: int,
        status: str,
        attempt_count: int,
        **context,
    ) -> None:
        LOGGER.info(
            "receipt job completed",
            extra={
                "context": {
                    "job_kind": kind,
                    "job_id": job_id,
                    "group_id": group_id,
                    "status": status,
                    "attempt_count": attempt_count,
                    **context,
                }
            },
        )

    @staticmethod
    def _log_failed(
        kind: str,
        job: ReceiptJob | TextReceiptJob,
        error_code: str,
        retryable: bool,
        retry_delay: float,
        extraction_attempt_number: int | None,
        processing_started_at: datetime | None,
    ) -> None:
        log = LOGGER.warning if retryable else LOGGER.error
        duration_ms = None
        if processing_started_at is not None:
            started = processing_started_at.replace(tzinfo=None)
            duration_ms = int(
                max((utc_now().replace(tzinfo=None) - started).total_seconds(), 0) * 1000
            )
        log(
            "receipt job retry scheduled" if retryable else "receipt job failed permanently",
            extra={
                "context": {
                    "job_kind": kind,
                    "job_id": job.id,
                    "group_id": job.group_id,
                    "evidence_id": job.evidence_id,
                    "error_code": error_code,
                    "attempt_count": job.attempt_count,
                    "extraction_attempt": extraction_attempt_number,
                    "duration_ms": duration_ms,
                    "retry_in_seconds": round(retry_delay, 3) if retryable else None,
                }
            },
        )

    async def _notify_one_due_job(self) -> bool:
        now = utc_now()
        cutoff = now - timedelta(seconds=self.delay_notification_seconds)
        terminal = ReceiptJobState.FAILED_FINAL.value
        active = {
            ReceiptJobState.QUEUED_DOWNLOAD.value,
            ReceiptJobState.DOWNLOADING.value,
            ReceiptJobState.DOWNLOAD_FAILED_RETRYABLE.value,
            ReceiptJobState.QUEUED_EXTRACTION.value,
            ReceiptJobState.EXTRACTING.value,
            ReceiptJobState.EXTRACTION_FAILED_RETRYABLE.value,
        }
        with self.database.session() as session:
            image = session.scalar(
                select(ReceiptJob)
                .where(
                    ReceiptJob.status == terminal,
                    ReceiptJob.failure_notified_at.is_(None),
                )
                .order_by(ReceiptJob.finished_at, ReceiptJob.id)
                .limit(1)
            )
            text = session.scalar(
                select(TextReceiptJob)
                .where(
                    TextReceiptJob.status == terminal,
                    TextReceiptJob.failure_notified_at.is_(None),
                )
                .order_by(TextReceiptJob.finished_at, TextReceiptJob.id)
                .limit(1)
            )
            candidates: list[tuple[str, ReceiptJob | TextReceiptJob, str]] = []
            if image is not None:
                candidates.append(("image", image, "failure"))
            if text is not None:
                candidates.append(("text", text, "failure"))
            if not candidates:
                image = session.scalar(
                    select(ReceiptJob)
                    .where(
                        ReceiptJob.status.in_(active),
                        ReceiptJob.created_at <= cutoff,
                        ReceiptJob.delayed_notified_at.is_(None),
                    )
                    .order_by(ReceiptJob.created_at, ReceiptJob.id)
                    .limit(1)
                )
                text = session.scalar(
                    select(TextReceiptJob)
                    .where(
                        TextReceiptJob.status.in_(active),
                        TextReceiptJob.created_at <= cutoff,
                        TextReceiptJob.delayed_notified_at.is_(None),
                    )
                    .order_by(TextReceiptJob.created_at, TextReceiptJob.id)
                    .limit(1)
                )
                if image is not None:
                    candidates.append(("image", image, "delay"))
                if text is not None:
                    candidates.append(("text", text, "delay"))
            if not candidates:
                return False
            kind, job, notification = min(
                candidates, key=lambda item: (item[1].created_at, item[1].id)
            )
            job_id = job.id
            group_id = job.group_id
            group = session.get(TelegramGroup, group_id)
            authorized = self.access_policy.authorizes_group(group)
            destinations: list[int] = []
            fallback_chat_id: int | None = None
            if group is not None:
                fallback_chat_id = group.telegram_chat_id
                settings = group.settings if isinstance(group.settings, dict) else {}
                authorizer_id = settings.get("access_authorized_by_telegram_user_id")
                if isinstance(authorizer_id, int):
                    destinations.append(authorizer_id)
                destinations.extend(
                    session.scalars(
                        select(TelegramUser.telegram_user_id)
                        .join(GroupMembership, GroupMembership.user_id == TelegramUser.id)
                        .where(
                            GroupMembership.group_id == group_id,
                            GroupMembership.active.is_(True),
                            GroupMembership.role == MembershipRole.ACCOUNTANT.value,
                        )
                    )
                )
            destinations = list(dict.fromkeys(destinations))

        delivered = False
        if authorized:
            message = (
                texts.receipt_processing_failed(kind=kind, job_id=job_id)
                if notification == "failure"
                else texts.receipt_processing_delayed(kind=kind, job_id=job_id)
            )
            for chat_id in destinations:
                try:
                    await self.gateway.send_message(chat_id, message)
                    delivered = True
                except TelegramAPIError:
                    LOGGER.warning(
                        "receipt notification delivery failed",
                        extra={
                            "context": {
                                "job_kind": kind,
                                "job_id": job_id,
                                "group_id": group_id,
                                "notification": notification,
                            }
                        },
                    )
            if not delivered and fallback_chat_id is not None:
                try:
                    await self.gateway.send_message(fallback_chat_id, message)
                    delivered = True
                except TelegramAPIError:
                    LOGGER.warning(
                        "receipt notification fallback failed",
                        extra={
                            "context": {
                                "job_kind": kind,
                                "job_id": job_id,
                                "group_id": group_id,
                                "notification": notification,
                            }
                        },
                    )

        with self.database.session() as session:
            repository = (
                TextReceiptJobRepository(session, group_id)
                if kind == "text"
                else ReceiptJobRepository(session, group_id)
            )
            persisted = repository.get(job_id)
            if notification == "failure":
                persisted.failure_notified_at = now
            else:
                persisted.delayed_notified_at = now
            session.flush()
        LOGGER.info(
            "receipt notification handled",
            extra={
                "context": {
                    "job_kind": kind,
                    "job_id": job_id,
                    "group_id": group_id,
                    "notification": notification,
                    "delivered": delivered,
                }
            },
        )
        return True
