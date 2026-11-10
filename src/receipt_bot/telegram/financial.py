from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from typing import Any

from sqlalchemy import func, or_, select

from receipt_bot.domain.enums import (
    AllocationState,
    CurrencyUnit,
    PaymentEvidenceState,
    PaymentDateState,
    ReviewDecision,
)
from receipt_bot.domain.money import (
    Money,
    format_rial_as_toman,
    normalize_digits,
    parse_integer,
)
from receipt_bot.domain.request_parser import (
    normalize_request_identifier,
    parse_request_announcement,
)
from receipt_bot.domain.time import TEHRAN, UTC, to_tehran, utc_now
from receipt_bot.persistence.database import Database
from receipt_bot.persistence.models import (
    Counterparty,
    ExtractionAttempt,
    Payment,
    PaymentEvidence,
    PaymentPromise,
    PaymentRequest,
    ReceiptImage,
    ReceiptJob,
    RequestPaymentAllocation,
    TelegramGroup,
    TelegramMessage,
    TelegramUser,
    TextReceiptJob,
)
from receipt_bot.persistence.repositories import InteractionRepository
from receipt_bot.reconciliation import LedgerService
from receipt_bot.reconciliation.service import COUNTABLE_DATE_STATES, payment_date_is_eligible
from receipt_bot.reporting import ReportingEngine, render_persian_report

PAGE_SIZE = 5
ACTIVE_ALLOCATION_STATES = {
    AllocationState.PROPOSED.value,
    AllocationState.CONFIRMED.value,
}
COUNTABLE_PAYMENT_STATES = {
    PaymentEvidenceState.SUBMITTED.value,
    PaymentEvidenceState.NEEDS_REVIEW.value,
    PaymentEvidenceState.CONFIRMED.value,
}
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

_PAYMENT_STATES = {
    "submitted": "ثبت‌شده / تأییدنشده",
    "needs_review": "نیازمند بررسی",
    "confirmed": "تأیید حسابداری",
    "rejected": "ردشده",
    "duplicate": "تکراری",
}
_DATE_STATES = {
    "valid": "تاریخ معتبر و قابل محاسبه",
    "needs_review_old": "تاریخ قدیمی؛ تا تأیید حسابدار از جمع‌ها خارج است",
    "needs_review_future": "تاریخ آینده؛ تا تأیید حسابدار از جمع‌ها خارج است",
    "needs_review_unknown": "تاریخ نامشخص/نامعتبر؛ تا تأیید حسابدار از جمع‌ها خارج است",
    "approved_exception": "استثنای تاریخ با تأیید حسابدار",
    "rejected": "تاریخ ردشده؛ از جمع‌ها خارج است",
    "legacy_unassessed": "سابقه قدیمی؛ بدون بازطبقه‌بندی",
}
_ALLOCATION_STATES = {
    "proposed": "پیشنهادی",
    "confirmed": "تأییدشده",
    "cancelled": "لغوشده",
    "rejected": "ردشده",
}
_STORAGE_STATES = {
    "present": "تصویر موجود است",
    "deduplicated": "نسخه تکراری؛ فایل جداگانه ندارد",
    "deleted": "تصویر اصلی طبق سیاست نگهداری حذف شده است",
    "missing": "تصویر اصلی در فضای ذخیره‌سازی یافت نشد",
    "delete_failed": "حذف تصویر ناموفق یا ناامن بود",
}
_JOB_STATES = {
    "queued_download": "در صف دریافت تصویر",
    "downloading": "در حال دریافت تصویر",
    "download_failed_retryable": "تلاش مجدد برای دریافت تصویر",
    "queued_extraction": "در صف استخراج اطلاعات",
    "extracting": "در حال استخراج اطلاعات",
    "extraction_failed_retryable": "تلاش مجدد پس از اختلال پردازش",
    "succeeded": "پردازش تکمیل شده",
    "duplicate_exact": "تکراری؛ دوباره محاسبه نمی‌شود",
    "failed_final": "پردازش ناموفق نهایی؛ نیازمند بررسی",
}
_UNFINISHED_JOB_STATES = {
    "queued_download",
    "downloading",
    "download_failed_retryable",
    "queued_extraction",
    "extracting",
    "extraction_failed_retryable",
    "failed_final",
}


class FinancialInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReceiptProcessingCounts:
    queued: int = 0
    processing: int = 0
    retrying: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.queued + self.processing + self.retrying + self.failed

    def warning(self) -> str | None:
        if not self.total:
            return None
        return (
            "⚠️ هشدار کامل‌بودن گزارش: "
            f"{self.total} رسید هنوز وارد محاسبات مالی نشده است "
            f"(در صف {self.queued}، در حال پردازش {self.processing}، "
            f"تلاش مجدد {self.retrying}، ناموفق {self.failed}). "
            "تا تعیین تکلیف این موارد، جمع‌ها ممکن است ناقص باشند."
        ).translate(_PERSIAN_DIGITS)


@dataclass(frozen=True, slots=True)
class Period:
    start_date: date
    days: int = 1

    @property
    def end_date(self) -> date:
        return self.start_date + timedelta(days=self.days)

    @property
    def utc_bounds(self) -> tuple[datetime, datetime]:
        start = datetime.combine(self.start_date, time.min, tzinfo=TEHRAN)
        end = datetime.combine(self.end_date, time.min, tzinfo=TEHRAN)
        return start.astimezone(UTC), end.astimezone(UTC)

    def to_state(self) -> dict[str, Any]:
        return {"start_date": self.start_date.isoformat(), "days": self.days}

    @classmethod
    def today(cls, now: datetime | None = None) -> Period:
        current = to_tehran(now or utc_now()).date()
        return cls(current, 1)

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> Period:
        try:
            start = date.fromisoformat(str(state["start_date"]))
            days = int(state.get("days", 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise FinancialInputError("invalid period state") from exc
        if days not in {1, 7, 30}:
            raise FinancialInputError("unsupported period length")
        return cls(start, days)

    def shifted(self, direction: int) -> Period:
        return Period(self.start_date + timedelta(days=self.days * direction), self.days)


def period_from_argument(argument: str | None, *, now: datetime | None = None) -> Period:
    if not argument:
        return Period.today(now)
    value = normalize_digits(argument).strip().replace("/", "-")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise FinancialInputError("تاریخ میلادی باید به شکل YYYY-MM-DD باشد.") from exc
    if not 2000 <= parsed.year <= 2100:
        raise FinancialInputError(
            "تاریخ دستوری باید میلادی و به شکل YYYY-MM-DD باشد؛ برای انتخاب بدون تایپ از دکمه‌ها استفاده کنید."
        )
    return Period(parsed, 1)


def parse_reason(text: str | None) -> str:
    reason = " ".join((text or "").strip().split())
    if len(reason) < 3:
        raise FinancialInputError("دلیل باید حداقل سه نویسه داشته باشد.")
    if len(reason) > 500:
        raise FinancialInputError("دلیل نباید بیشتر از ۵۰۰ نویسه باشد.")
    return reason


def parse_money_and_reason(text: str | None) -> tuple[Money, str]:
    raw = (text or "").strip()
    parts = raw.split("|", 1)
    if len(parts) != 2:
        raise FinancialInputError("مبلغ و دلیل را با علامت | جدا کنید.")
    money_text, reason_text = (part.strip() for part in parts)
    tokens = money_text.split()
    if len(tokens) < 2:
        raise FinancialInputError("مبلغ باید همراه واحد ریال یا تومان باشد.")
    unit_text = tokens[-1].casefold()
    unit = {
        "ریال": CurrencyUnit.RIAL,
        "rial": CurrencyUnit.RIAL,
        "تومان": CurrencyUnit.TOMAN,
        "toman": CurrencyUnit.TOMAN,
    }.get(unit_text)
    if unit is None:
        raise FinancialInputError("واحد مبلغ فقط ریال یا تومان است.")
    try:
        money = Money(parse_integer(" ".join(tokens[:-1])), unit)
    except (TypeError, ValueError) as exc:
        raise FinancialInputError("مبلغ معتبر نیست.") from exc
    if money.value == 0:
        raise FinancialInputError("مبلغ باید بیشتر از صفر باشد.")
    return money, parse_reason(reason_text)


def parse_date_and_reason(text: str | None) -> tuple[str, str]:
    raw = (text or "").strip()
    parts = raw.split("|", 1)
    if len(parts) != 2:
        raise FinancialInputError("تاریخ و دلیل را با علامت | جدا کنید.")
    date_text, reason_text = (part.strip() for part in parts)
    if not date_text or len(date_text) > 128:
        raise FinancialInputError("تاریخ اصلاح‌شده معتبر نیست.")
    return date_text, parse_reason(reason_text)


def parse_request_review_input(text: str | None) -> dict[str, Any]:
    raw = (text or "").strip()
    parts = [part.strip() for part in raw.split("|", 3)]
    if len(parts) != 4:
        raise FinancialInputError(
            "مبلغ، نام صاحب حساب، مشخصات حساب و دلیل را با | جدا کنید."
        )
    money_text, holder, details_text, reason_text = parts
    money, _ = parse_money_and_reason(f"{money_text} | {reason_text}")
    holder = " ".join(holder.split())
    if not 2 <= len(holder) <= 255:
        raise FinancialInputError("نام صاحب حساب باید روشن و قابل بررسی باشد.")
    details: dict[str, str | None] = {
        "destination_iban": None,
        "destination_card": None,
        "destination_account": None,
        "destination_bank": None,
    }
    labels = {
        "شبا": "destination_iban",
        "iban": "destination_iban",
        "کارت": "destination_card",
        "card": "destination_card",
        "حساب": "destination_account",
        "account": "destination_account",
        "بانک": "destination_bank",
        "bank": "destination_bank",
    }
    for segment in re.split(r"[؛;]", details_text):
        if not segment.strip():
            continue
        if ":" not in segment:
            raise FinancialInputError("نوع مشخصات حساب را مانند شبا: یا کارت: بنویسید.")
        label, value = (item.strip() for item in segment.split(":", 1))
        field = labels.get(label.casefold())
        if field is None or details[field] is not None:
            raise FinancialInputError("نوع مشخصات حساب ناشناخته یا تکراری است.")
        try:
            details[field] = normalize_request_identifier(label, value)
        except ValueError as exc:
            raise FinancialInputError("شماره حساب، کارت یا شبا معتبر نیست.") from exc
    if not any(details[key] for key in ("destination_iban", "destination_card", "destination_account")):
        raise FinancialInputError("حداقل یک شماره شبا، کارت یا حساب معتبر لازم است.")
    return {
        "amount_value": money.value,
        "amount_unit": money.unit.value,
        "account_holder": holder,
        **details,
        "reason": parse_reason(reason_text),
    }


def toman(rial: int | None) -> str:
    if rial is None:
        return "نامشخص"
    return f"{format_rial_as_toman(rial)} تومان"


def source_link(chat_id: int, message_id: int) -> str:
    raw = str(chat_id)
    if raw.startswith("-100") and len(raw) > 4:
        return f"https://t.me/c/{raw[4:]}/{message_id}"
    return f"شناسه گروه {chat_id}، پیام {message_id}"


def split_telegram_text(text: str, *, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if current and size + len(line) > limit:
            chunks.append("".join(current).rstrip())
            current, size = [], 0
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current).rstrip())
    return chunks


class FinancialWorkflowService:
    def __init__(self, database: Database, *, receipt_max_age_days: int = 30) -> None:
        self.database = database
        self.receipt_max_age_days = receipt_max_age_days

    @staticmethod
    def _group(session, group_id: int) -> TelegramGroup:
        group = session.get(TelegramGroup, group_id)
        if group is None:
            raise LookupError("group does not exist")
        return group

    @staticmethod
    def _payment_identity(session, payment: Payment) -> tuple[Counterparty | None, TelegramUser | None]:
        party = (
            session.scalar(
                select(Counterparty).where(
                    Counterparty.id == payment.counterparty_id,
                    Counterparty.group_id == payment.group_id,
                )
            )
            if payment.counterparty_id is not None
            else None
        )
        message = (
            session.scalar(
                select(TelegramMessage).where(
                    TelegramMessage.id == payment.source_message_id,
                    TelegramMessage.group_id == payment.group_id,
                )
            )
            if payment.source_message_id is not None
            else None
        )
        sender = (
            session.get(TelegramUser, message.sender_user_id)
            if message is not None and message.sender_user_id is not None
            else None
        )
        return party, sender

    @staticmethod
    def _payment_identity_label(
        party: Counterparty | None, sender: TelegramUser | None
    ) -> str:
        if party is not None:
            return f"پرداخت‌کننده {party.display_name}"
        if sender is not None:
            return (
                f"فرستنده تلگرام {sender.display_name}؛ "
                "پرداخت‌کننده تعیین‌نشده"
            )
        return "پرداخت‌کننده تعیین‌نشده"

    def report(self, group_id: int, user_id: int, period: Period) -> str:
        start, end = period.utc_bounds
        with self.database.session() as session:
            engine = ReportingEngine(session, group_id)
            projection = engine.build(period_start=start, period_end=end)
            rendered = render_persian_report(projection)
            warning = self._processing_counts(session, group_id, start, end).warning()
            if warning:
                rendered = f"{rendered}\n\n{warning}"
            engine.record_run(
                projection,
                requested_by_user_id=user_id,
                rendered_output=rendered,
            )
            return rendered

    def summary(self, group_id: int, period: Period) -> str:
        start, end = period.utc_bounds
        with self.database.session() as session:
            report = ReportingEngine(session, group_id).build(period_start=start, period_end=end)
        totals = report.totals
        label = self._period_label(period)
        lines = [
            f"خلاصه مالی — {label}",
            f"درخواست قابل مقایسه: {toman(totals.requested_rial)}",
            f"رسید ثبت‌شده: {toman(totals.submitted_rial)}",
            f"رسید تأیید حسابداری‌شده: {toman(totals.evidence_confirmed_rial)}",
            f"تخصیص تأییدشده به درخواست: {toman(totals.confirmed_rial)}",
            f"تأییدنشده از نظر اتصال/تخصیص: {toman(totals.unverified_rial)}",
            f"اختلاف قابل مقایسه: {toman(totals.documented_difference_rial)}",
            f"وعده پرداخت (خارج از پرداخت‌ها): {toman(totals.promised_rial)}",
            f"موارد حل‌نشده/مستثنا: {totals.unresolved_count}",
            "",
            "رسید به معنی وصول بانکی نیست و اختلاف درخواست، بدهی کلی شخص محسوب نمی‌شود.",
        ]
        with self.database.session() as session:
            warning = self._processing_counts(session, group_id, start, end).warning()
        if warning:
            lines.extend(["", warning])
        date_exclusions = [
            item for item in report.exclusions if item.code.startswith("payment_date_")
        ]
        if date_exclusions:
            lines.extend(
                [
                    "",
                    f"⚠️ {len(date_exclusions)} رسید به‌دلیل تاریخ قدیمی، آینده یا نامعتبر در جمع‌ها منظور نشده است.",
                ]
            )
        return "\n".join(lines).translate(_PERSIAN_DIGITS)

    def capture_source(
        self,
        group_id: int,
        *,
        telegram_message_id: int,
        operation: str,
    ) -> dict[str, Any]:
        if operation not in {"request_create", "promise_create"}:
            raise FinancialInputError("نوع ثبت مالی نامعتبر است.")
        with self.database.session() as session:
            source = session.scalar(
                select(TelegramMessage).where(
                    TelegramMessage.group_id == group_id,
                    TelegramMessage.telegram_message_id == telegram_message_id,
                )
            )
            if source is None:
                raise FinancialInputError(
                    "پیام اصلی در سوابق ربات نیست؛ فقط پیام‌های دریافت‌شده پس از فعال‌سازی قابل ثبت‌اند."
                )
            if source.sender_user_id is None:
                raise FinancialInputError("فرستنده پیام اصلی قابل شناسایی نیست.")
            request_id = session.scalar(
                select(PaymentRequest.id).where(
                    PaymentRequest.group_id == group_id,
                    PaymentRequest.source_message_id == source.id,
                )
            )
            promise_id = session.scalar(
                select(PaymentPromise.id).where(
                    PaymentPromise.group_id == group_id,
                    PaymentPromise.source_message_id == source.id,
                )
            )
            if request_id is not None or promise_id is not None:
                raise FinancialInputError("این پیام قبلاً به‌عنوان درخواست یا وعده ثبت شده است.")
            sender = session.get(TelegramUser, source.sender_user_id)
            if sender is None:
                raise FinancialInputError("فرستنده پیام اصلی در سوابق کاربران وجود ندارد.")
            state: dict[str, Any] = {
                "operation": operation,
                "source_message_id": source.id,
                "source_sender_user_id": sender.id,
                "source_sender_name": sender.display_name,
            }
            if operation == "request_create":
                candidate = parse_request_announcement(source.text)
                parsed = candidate.as_state()
                state.update(parsed)
                state["source_parser_candidate"] = parsed
                state["request_prefill_complete"] = candidate.complete_for_request
                state["request_manual_legacy"] = not any(
                    (
                        candidate.amount_value,
                        candidate.account_holder,
                        candidate.destination_iban,
                        candidate.destination_card,
                        candidate.destination_account,
                        candidate.ambiguous_fields,
                    )
                )
            return state

    @staticmethod
    def _masked(value: str | None) -> str:
        if not value:
            return "شناسایی نشد"
        compact = value.replace(" ", "")
        return ("*" * max(len(compact) - 4, 4)) + compact[-4:]

    @classmethod
    def request_prefill_preview(cls, state: dict[str, Any]) -> str:
        amount = None
        if state.get("amount_value") is not None:
            try:
                amount = Money(
                    int(state["amount_value"]),
                    CurrencyUnit(str(state.get("amount_unit", "unknown"))),
                )
            except (TypeError, ValueError):
                amount = None
        details = next(
            (
                cls._masked(str(state[key]))
                for key in ("destination_iban", "destination_card", "destination_account")
                if state.get(key)
            ),
            "شناسایی نشد",
        )
        ambiguous = state.get("ambiguous_fields") or []
        lines = [
            "پیش‌نمایش درخواست استخراج‌شده",
            f"مبلغ: {toman(amount.canonical_rial) if amount else 'نامشخص/مبهم'}",
            f"صاحب حساب: {state.get('account_holder') or 'نامشخص/مبهم'}",
            f"مشخصات حساب: {details}",
            f"بانک: {state.get('destination_bank') or 'شناسایی نشد'}",
            f"فرستنده پیام: {state.get('source_sender_name') or 'نامشخص'} (صرفاً منبع پیام)",
        ]
        if ambiguous:
            lines.append("⚠️ بعضی فیلدها چند مقدار محتمل دارند و باید اصلاح شوند.")
        lines.append("شماره کامل حساب در گروه نمایش داده نمی‌شود.")
        return "\n".join(lines).translate(_PERSIAN_DIGITS)

    def receipts(self, group_id: int, period: Period, page: int = 0) -> tuple[str, list[int], bool, bool]:
        start, end = period.utc_bounds
        with self.database.session() as session:
            query = (
                select(TelegramMessage, ReceiptJob, TextReceiptJob, PaymentEvidence, Payment)
                .outerjoin(ReceiptJob, ReceiptJob.source_message_id == TelegramMessage.id)
                .outerjoin(TextReceiptJob, TextReceiptJob.source_message_id == TelegramMessage.id)
                .outerjoin(PaymentEvidence, PaymentEvidence.source_message_id == TelegramMessage.id)
                .outerjoin(Payment, Payment.evidence_id == PaymentEvidence.id)
                .where(
                    TelegramMessage.group_id == group_id,
                    TelegramMessage.occurred_at >= start,
                    TelegramMessage.occurred_at < end,
                    or_(ReceiptJob.id.is_not(None), TextReceiptJob.id.is_not(None)),
                )
                .order_by(TelegramMessage.occurred_at.desc(), TelegramMessage.id.desc())
            )
            page = max(page, 0)
            job_rows = list(session.execute(query))
            entries: list[tuple[datetime, str, Any]] = [
                (row[0].occurred_at, "job", row) for row in job_rows
            ]
            job_payment_ids = {row[4].id for row in job_rows if row[4] is not None}
            manual_payments = list(
                session.scalars(
                    select(Payment).where(
                        Payment.group_id == group_id,
                        Payment.created_at >= start,
                        Payment.created_at < end,
                        Payment.id.not_in(job_payment_ids) if job_payment_ids else True,
                    )
                )
            )
            entries.extend((payment.created_at, "payment", payment) for payment in manual_payments)
            entries.sort(key=lambda item: (item[0], getattr(item[2], "id", 0)), reverse=True)
            first = page * PAGE_SIZE
            rows = entries[first : first + PAGE_SIZE]
            has_previous, has_next = page > 0, first + PAGE_SIZE < len(entries)
            lines = [f"رسیدها — {self._period_label(period)}"]
            ids: list[int] = []
            for _occurred_at, entry_kind, entry in rows:
                if entry_kind == "payment":
                    payment = entry
                    ids.append(payment.id)
                    party, sender = self._payment_identity(session, payment)
                    receipt = session.get(ReceiptImage, payment.receipt_id) if payment.receipt_id else None
                    kind = "تصویر" if receipt is not None else "ثبت دستی/متن"
                    storage = (
                        _STORAGE_STATES.get(receipt.storage_state, receipt.storage_state)
                        if receipt
                        else "بدون فایل تصویر"
                    )
                    lines.append(
                        f"• رسید #{payment.id} ({kind}): {toman(payment.amount_rial)}؛ "
                        f"{self._payment_identity_label(party, sender)}؛ "
                        f"وضعیت {_PAYMENT_STATES.get(payment.evidence_state, payment.evidence_state)}؛ "
                        f"پردازش ثبت‌شده؛ تاریخ {_DATE_STATES.get(payment.date_eligibility_state, payment.date_eligibility_state)}؛ تصویر {storage}."
                    )
                    continue
                _message, image_job, text_job, _evidence, payment = entry
                job = image_job or text_job
                kind = "تصویر" if image_job is not None else "متن"
                state = _JOB_STATES.get(job.status, job.status)
                if payment is None:
                    lines.append(
                        f"• ورودی {kind} #{job.id}: مبلغ هنوز استخراج نشده؛ "
                        f"وضعیت {state}؛ تعداد تلاش {job.attempt_count}."
                    )
                    continue
                ids.append(payment.id)
                party, sender = self._payment_identity(session, payment)
                receipt = session.get(ReceiptImage, payment.receipt_id) if payment.receipt_id else None
                storage = (
                    _STORAGE_STATES.get(receipt.storage_state, receipt.storage_state)
                    if receipt
                    else "بدون فایل تصویر"
                )
                lines.append(
                    f"• رسید #{payment.id} ({kind}): {toman(payment.amount_rial)}؛ "
                    f"{self._payment_identity_label(party, sender)}؛ "
                    f"وضعیت {_PAYMENT_STATES.get(payment.evidence_state, payment.evidence_state)}؛ "
                    f"پردازش {state}؛ تاریخ {_DATE_STATES.get(payment.date_eligibility_state, payment.date_eligibility_state)}؛ تصویر {storage}."
                )
            if not rows:
                lines.append("موردی در این بازه ثبت نشده است.")
        lines.append("جزئیات حساب/مرجع فقط در گفت‌وگوی خصوصی مجاز نمایش داده می‌شود.")
        return "\n".join(lines).translate(_PERSIAN_DIGITS), ids, has_previous, has_next

    def pending(self, group_id: int, page: int = 0) -> tuple[str, list[int], list[int], bool, bool]:
        with self.database.session() as session:
            payments = list(
                session.scalars(
                    select(Payment)
                    .where(
                        Payment.group_id == group_id,
                        or_(
                            Payment.evidence_state.in_(["submitted", "needs_review"]),
                            Payment.date_eligibility_state.in_(
                                [
                                    PaymentDateState.NEEDS_REVIEW_OLD.value,
                                    PaymentDateState.NEEDS_REVIEW_FUTURE.value,
                                    PaymentDateState.NEEDS_REVIEW_UNKNOWN.value,
                                ]
                            ),
                        ),
                    )
                    .order_by(Payment.created_at, Payment.id)
                )
            )
            allocations = list(
                session.scalars(
                    select(RequestPaymentAllocation)
                    .where(
                        RequestPaymentAllocation.group_id == group_id,
                        RequestPaymentAllocation.state == AllocationState.PROPOSED.value,
                    )
                    .order_by(RequestPaymentAllocation.created_at, RequestPaymentAllocation.id)
                )
            )
            failed_images = list(
                session.scalars(
                    select(ReceiptJob)
                    .where(
                        ReceiptJob.group_id == group_id,
                        ReceiptJob.status == "failed_final",
                    )
                    .order_by(ReceiptJob.created_at, ReceiptJob.id)
                )
            )
            failed_texts = list(
                session.scalars(
                    select(TextReceiptJob)
                    .where(
                        TextReceiptJob.group_id == group_id,
                        TextReceiptJob.status == "failed_final",
                    )
                    .order_by(TextReceiptJob.created_at, TextReceiptJob.id)
                )
            )
            combined = [("payment", item) for item in payments] + [
                ("allocation", item) for item in allocations
            ] + [("failed_image", item) for item in failed_images] + [
                ("failed_text", item) for item in failed_texts
            ]
            start = max(page, 0) * PAGE_SIZE
            selected = combined[start : start + PAGE_SIZE]
            payment_ids = [item.id for kind, item in selected if kind == "payment"]
            allocation_ids = [item.id for kind, item in selected if kind == "allocation"]
            lines = ["موارد نیازمند بررسی حسابداری"]
            for kind, item in selected:
                if kind == "payment":
                    lines.append(
                        f"• رسید #{item.id}: {toman(item.amount_rial)}؛ "
                        f"{_PAYMENT_STATES.get(item.evidence_state, item.evidence_state)}؛ "
                        f"{_DATE_STATES.get(item.date_eligibility_state, item.date_eligibility_state)}."
                    )
                else:
                    if kind == "allocation":
                        lines.append(
                            f"• تخصیص #{item.id}: {toman(item.allocated_rial)}؛ "
                            f"رسید #{item.payment_id} ← درخواست #{item.request_id}."
                        )
                    else:
                        label = "تصویری" if kind == "failed_image" else "متنی"
                        lines.append(
                            f"• ورودی {label} #{item.id}: پردازش ناموفق نهایی؛ "
                            "اصل ورودی محفوظ است و نیازمند بررسی حسابدار است."
                        )
            if not selected:
                lines.append("مورد بازی برای بررسی وجود ندارد.")
            return (
                "\n".join(lines).translate(_PERSIAN_DIGITS),
                payment_ids,
                allocation_ids,
                page > 0,
                start + PAGE_SIZE < len(combined),
            )

    def transactions(
        self, group_id: int, period: Period, page: int = 0
    ) -> tuple[str, list[int], bool, bool]:
        start, end = period.utc_bounds
        with self.database.session() as session:
            items: list[tuple[datetime, int, str, str]] = []
            for request in session.scalars(
                select(PaymentRequest).where(
                    PaymentRequest.group_id == group_id,
                    PaymentRequest.requested_at >= start,
                    PaymentRequest.requested_at < end,
                )
            ):
                party = session.get(Counterparty, request.counterparty_id)
                linked = list(
                    session.scalars(
                        select(RequestPaymentAllocation.payment_id).where(
                            RequestPaymentAllocation.group_id == group_id,
                            RequestPaymentAllocation.request_id == request.id,
                            RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
                        )
                    )
                )
                link_text = (
                    "، رسیدهای " + "، ".join(f"#{item}" for item in linked)
                    if linked
                    else "، بدون رسید متصل"
                )
                items.append(
                    (
                        request.requested_at,
                        request.id,
                        "request",
                        (
                            f"درخواست #{request.id} — {party.display_name if party else 'نامشخص'} — "
                            f"{toman(request.amount_rial)} — {request.state}{link_text}"
                        ),
                    )
                )
            for payment in session.scalars(
                select(Payment).where(
                    Payment.group_id == group_id,
                    Payment.created_at >= start,
                    Payment.created_at < end,
                )
            ):
                party, sender = self._payment_identity(session, payment)
                linked = list(
                    session.scalars(
                        select(RequestPaymentAllocation.request_id).where(
                            RequestPaymentAllocation.group_id == group_id,
                            RequestPaymentAllocation.payment_id == payment.id,
                            RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
                        )
                    )
                )
                link_text = (
                    "، درخواست‌های " + "، ".join(f"#{item}" for item in linked)
                    if linked
                    else "، تخصیص‌نیافته"
                )
                items.append(
                    (
                        payment.created_at,
                        payment.id,
                        "payment",
                        (
                            f"رسید #{payment.id} — {toman(payment.amount_rial)} — "
                            f"{self._payment_identity_label(party, sender)} — "
                            f"{_PAYMENT_STATES.get(payment.evidence_state, payment.evidence_state)}"
                            f"{link_text}"
                        ),
                    )
                )
            for promise in session.scalars(
                select(PaymentPromise).where(
                    PaymentPromise.group_id == group_id,
                    PaymentPromise.created_at >= start,
                    PaymentPromise.created_at < end,
                )
            ):
                party = session.get(Counterparty, promise.counterparty_id)
                items.append(
                    (
                        promise.created_at,
                        promise.id,
                        "promise",
                        f"وعده #{promise.id} — {party.display_name if party else 'نامشخص'} — "
                        f"{toman(promise.amount_rial)} — {promise.state}"
                        + (f"، درخواست #{promise.request_id}" if promise.request_id else ""),
                    )
                )
            items.sort(key=lambda item: (item[0], item[1]), reverse=True)
            offset = max(page, 0) * PAGE_SIZE
            selected = items[offset : offset + PAGE_SIZE]
        lines = [f"تراکنش‌ها — {self._period_label(period)}"]
        lines.extend(f"• {item[3]}" for item in selected)
        if not selected:
            lines.append("موردی در این بازه ثبت نشده است.")
        return (
            "\n".join(lines).translate(_PERSIAN_DIGITS),
            [item[1] for item in selected if item[2] == "payment"],
            page > 0,
            offset + PAGE_SIZE < len(items),
        )

    def counterparties(self, group_id: int, page: int = 0) -> tuple[str, list[int], bool, bool]:
        with self.database.session() as session:
            query = (
                select(Counterparty)
                .where(Counterparty.group_id == group_id, Counterparty.active.is_(True))
                .order_by(Counterparty.display_name, Counterparty.id)
            )
            rows, previous, following = self._page(session, query, page)
            names = [f"• #{item.id} — {item.display_name}" for item in rows]
            ids = [item.id for item in rows]
        lines = ["شخص موردنظر برای نمایش وضعیت مالی را انتخاب کنید.", *names]
        if not rows:
            lines.append("شخصی ثبت نشده است.")
        return "\n".join(lines).translate(_PERSIAN_DIGITS), ids, previous, following

    def balance(self, group_id: int, counterparty_id: int) -> str:
        with self.database.session() as session:
            party = session.scalar(
                select(Counterparty).where(
                    Counterparty.id == counterparty_id,
                    Counterparty.group_id == group_id,
                )
            )
            if party is None:
                raise LookupError("counterparty not found")
            requests = list(
                session.scalars(
                    select(PaymentRequest).where(
                        PaymentRequest.group_id == group_id,
                        PaymentRequest.counterparty_id == counterparty_id,
                        PaymentRequest.state != "cancelled",
                    )
                )
            )
            comparable_requests = [item for item in requests if item.amount_rial is not None]
            unknown_count = len(requests) - len(comparable_requests)
            request_ids = [item.id for item in requests]
            allocations = list(
                session.scalars(
                    select(RequestPaymentAllocation).where(
                        RequestPaymentAllocation.group_id == group_id,
                        RequestPaymentAllocation.request_id.in_(request_ids or [-1]),
                        RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
                    )
                )
            )
            submitted = 0
            confirmed = 0
            for allocation in allocations:
                payment = session.get(Payment, allocation.payment_id)
                if (
                    payment is None
                    or payment.group_id != group_id
                    or payment.evidence_state not in COUNTABLE_PAYMENT_STATES
                    or not payment_date_is_eligible(payment)
                ):
                    continue
                submitted += allocation.allocated_rial
                if payment.evidence_state == "confirmed" and allocation.state == "confirmed":
                    confirmed += allocation.allocated_rial
            unallocated = int(
                session.scalar(
                    select(func.coalesce(func.sum(Payment.amount_rial), 0)).where(
                        Payment.group_id == group_id,
                        Payment.counterparty_id == counterparty_id,
                        Payment.evidence_state.in_(COUNTABLE_PAYMENT_STATES),
                        Payment.date_eligibility_state.in_(COUNTABLE_DATE_STATES),
                        Payment.amount_rial.is_not(None),
                        ~Payment.id.in_(
                            select(RequestPaymentAllocation.payment_id).where(
                                RequestPaymentAllocation.group_id == group_id,
                                RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
                            )
                        ),
                    )
                )
                or 0
            )
            requested = sum(item.amount_rial or 0 for item in comparable_requests)
            promises = int(
                session.scalar(
                    select(func.coalesce(func.sum(PaymentPromise.amount_rial), 0)).where(
                        PaymentPromise.group_id == group_id,
                        PaymentPromise.counterparty_id == counterparty_id,
                        PaymentPromise.state == "open",
                        PaymentPromise.amount_rial.is_not(None),
                    )
                )
                or 0
            )
            excluded_date_count = int(
                session.scalar(
                    select(func.count(Payment.id)).where(
                        Payment.group_id == group_id,
                        Payment.counterparty_id == counterparty_id,
                        Payment.date_eligibility_state.not_in(COUNTABLE_DATE_STATES),
                    )
                )
                or 0
            )
        lines = [
            f"وضعیت ثبت‌شده برای {party.display_name}",
            f"جمع درخواست‌های قابل مقایسه: {toman(requested)}",
            f"رسید تخصیص‌یافته: {toman(submitted)}",
            f"تأیید حسابداری: {toman(confirmed)}",
            f"تخصیص‌یافته ولی تأییدنشده: {toman(submitted - confirmed)}",
            f"رسید منتسب ولی تخصیص‌نیافته: {toman(unallocated)}",
            f"اختلاف مستندی درخواست‌ها: {toman(requested - submitted)}",
            f"وعده باز (خارج از پرداخت‌ها): {toman(promises)}",
        ]
        if unknown_count:
            lines.append(f"⚠️ {unknown_count} درخواست مبلغ/واحد قابل اتکا ندارد.")
        if excluded_date_count:
            lines.append(
                f"⚠️ {excluded_date_count} رسید به‌دلیل وضعیت تاریخ در این مانده محاسبه نشده است."
            )
        lines.extend(
            [
                "",
                "⚠️ این خروجی بدهی قطعی یا مانده بانکی شخص نیست؛ فقط وضعیت اسناد ثبت‌شده را نشان می‌دهد.",
            ]
        )
        return "\n".join(lines).translate(_PERSIAN_DIGITS)

    def payment_detail(self, group_id: int, payment_id: int, *, private: bool = False) -> str:
        with self.database.session() as session:
            payment = session.scalar(
                select(Payment).where(Payment.id == payment_id, Payment.group_id == group_id)
            )
            if payment is None:
                raise LookupError("payment not found")
            party, sender = self._payment_identity(session, payment)
            evidence = session.get(PaymentEvidence, payment.evidence_id) if payment.evidence_id else None
            attempt = self._latest_attempt(session, evidence.id) if evidence else None
            receipt = session.get(ReceiptImage, payment.receipt_id) if payment.receipt_id else None
            message = session.get(TelegramMessage, payment.source_message_id) if payment.source_message_id else None
            group = self._group(session, group_id)
            allocated = int(
                session.scalar(
                    select(func.coalesce(func.sum(RequestPaymentAllocation.allocated_rial), 0)).where(
                        RequestPaymentAllocation.group_id == group_id,
                        RequestPaymentAllocation.payment_id == payment.id,
                        RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
                    )
                )
                or 0
            )
            lines = [
                f"رسید #{payment.id}",
                f"مبلغ: {toman(payment.amount_rial)}",
                f"پرداخت‌کننده: {party.display_name if party else 'تعیین‌نشده'}",
                f"فرستنده تلگرام: {sender.display_name if sender else 'نامشخص'}",
                f"وضعیت سند: {_PAYMENT_STATES.get(payment.evidence_state, payment.evidence_state)}",
                f"وضعیت تاریخ: {_DATE_STATES.get(payment.date_eligibility_state, payment.date_eligibility_state)}",
                f"وضعیت وصول مستقل: {payment.settlement_state}",
                f"تخصیص فعال: {toman(allocated)}",
                f"نوع مدرک: {evidence.evidence_kind if evidence else 'manual'}",
                f"وضعیت پردازش: {attempt.status if attempt else 'نامشخص/دستی'}",
                (
                    "فایل تصویر: "
                    f"{_STORAGE_STATES.get(receipt.storage_state, receipt.storage_state) if receipt else 'ندارد'}"
                ),
            ]
            if private:
                lines.extend(
                    [
                        f"تاریخ چاپ‌شده: {payment.printed_transaction_at or 'نامشخص'}",
                        f"تاریخ اصلاح‌شده: {payment.corrected_transaction_at or 'ندارد'}",
                        f"مرجع تراکنش: {payment.transaction_reference or 'نامشخص'}",
                    ]
                )
                candidate = attempt.normalized_candidate if attempt and attempt.normalized_candidate else {}
                labels = {
                    "source_name": "نام مبدأ",
                    "destination_name": "نام مقصد",
                    "source_card": "کارت مبدأ",
                    "destination_card": "کارت مقصد",
                    "source_iban": "شبا مبدأ",
                    "destination_iban": "شبا مقصد",
                    "bank_reference": "مرجع بانک",
                    "reference_number": "شماره ارجاع",
                    "document_number": "شماره سند",
                }
                for key, label in labels.items():
                    value = candidate.get(key)
                    if value:
                        lines.append(f"{label}: {value}")
                if message is not None:
                    lines.append(
                        f"منبع: {source_link(group.telegram_chat_id, message.telegram_message_id)}"
                    )
            else:
                lines.append("جزئیات حساب، مرجع و منبع فقط در گفت‌وگوی خصوصی نمایش داده می‌شود.")
        rendered = "\n".join(lines)
        return rendered if private else rendered.translate(_PERSIAN_DIGITS)

    def payment_date_state(self, group_id: int, payment_id: int) -> str:
        with self.database.session() as session:
            state = session.scalar(
                select(Payment.date_eligibility_state).where(
                    Payment.id == payment_id, Payment.group_id == group_id
                )
            )
            if state is None:
                raise LookupError("payment not found")
            return state

    def allocation_detail(self, group_id: int, allocation_id: int) -> str:
        with self.database.session() as session:
            item = session.scalar(
                select(RequestPaymentAllocation).where(
                    RequestPaymentAllocation.id == allocation_id,
                    RequestPaymentAllocation.group_id == group_id,
                )
            )
            if item is None:
                raise LookupError("allocation not found")
            return (
                f"تخصیص #{item.id}\n"
                f"رسید #{item.payment_id} ← درخواست #{item.request_id}\n"
                f"مبلغ: {toman(item.allocated_rial)}\n"
                f"وضعیت: {_ALLOCATION_STATES.get(item.state, item.state)}"
            ).translate(_PERSIAN_DIGITS)

    def open_requests(self, group_id: int, payment_id: int) -> list[tuple[int, str, int]]:
        with self.database.session() as session:
            payment = session.scalar(
                select(Payment).where(Payment.id == payment_id, Payment.group_id == group_id)
            )
            if payment is None or payment.amount_rial is None or not payment_date_is_eligible(payment):
                return []
            allocated_payment = self._allocated_for_payment(session, group_id, payment.id)
            payment_remaining = payment.amount_rial - allocated_payment
            requests = list(
                session.scalars(
                    select(PaymentRequest)
                    .where(
                        PaymentRequest.group_id == group_id,
                        PaymentRequest.state.not_in(["closed", "cancelled"]),
                        PaymentRequest.amount_rial.is_not(None),
                    )
                    .order_by(PaymentRequest.requested_at.desc(), PaymentRequest.id.desc())
                    .limit(10)
                )
            )
            result: list[tuple[int, str, int]] = []
            for request in requests:
                if payment.counterparty_id is not None and request.counterparty_id != payment.counterparty_id:
                    continue
                remaining = (request.amount_rial or 0) - self._allocated_for_request(session, group_id, request.id)
                maximum = min(payment_remaining, remaining)
                if maximum <= 0:
                    continue
                party = session.get(Counterparty, request.counterparty_id)
                result.append((request.id, party.display_name if party else "نامشخص", maximum))
            return result

    def counterparty_candidates(self, group_id: int, payment_id: int) -> list[tuple[int, str]]:
        with self.database.session() as session:
            payment = session.scalar(
                select(Payment).where(Payment.id == payment_id, Payment.group_id == group_id)
            )
            if payment is None:
                return []
            return [
                (item.id, item.display_name)
                for item in session.scalars(
                    select(Counterparty)
                    .where(
                        Counterparty.group_id == group_id,
                        Counterparty.active.is_(True),
                    )
                    .order_by(Counterparty.display_name, Counterparty.id)
                    .limit(20)
                )
            ]

    def canonical_payments(self, group_id: int, payment_id: int) -> list[tuple[int, int | None]]:
        with self.database.session() as session:
            return [
                (item.id, item.amount_rial)
                for item in session.scalars(
                    select(Payment)
                    .where(
                        Payment.group_id == group_id,
                        Payment.id != payment_id,
                        Payment.evidence_state != PaymentEvidenceState.DUPLICATE.value,
                    )
                    .order_by(Payment.created_at.desc(), Payment.id.desc())
                    .limit(10)
                )
            ]

    def apply_action(self, token: str, *, user_id: int, group_id: int) -> str:
        with self.database.session() as session:
            interactions = InteractionRepository(session)
            interaction = interactions.resolve(
                token,
                initiating_user_id=user_id,
                group_id=group_id,
            )
            if interaction is None or interaction.workflow != "finance_action":
                raise FinancialInputError("نشست عملیات منقضی یا قبلاً استفاده شده است.")
            state = interaction.state
            if state.get("phase") != "ready":
                raise FinancialInputError("اطلاعات عملیات هنوز کامل نشده است.")
            operation = str(state.get("operation"))
            reason = parse_reason(state.get("reason"))
            ledger = LedgerService(
                session,
                group_id,
                receipt_max_age_days=self.receipt_max_age_days,
            )
            if operation in {"request_create", "promise_create"}:
                source = session.scalar(
                    select(TelegramMessage).where(
                        TelegramMessage.id == int(state["source_message_id"]),
                        TelegramMessage.group_id == group_id,
                    )
                )
                sender = session.get(TelegramUser, int(state["source_sender_user_id"]))
                if source is None or sender is None or source.sender_user_id != sender.id:
                    raise FinancialInputError("منبع یا فرستنده پیام دیگر معتبر نیست.")
                existing_request = session.scalar(
                    select(PaymentRequest.id).where(
                        PaymentRequest.group_id == group_id,
                        PaymentRequest.source_message_id == source.id,
                    )
                )
                existing_promise = session.scalar(
                    select(PaymentPromise.id).where(
                        PaymentPromise.group_id == group_id,
                        PaymentPromise.source_message_id == source.id,
                    )
                )
                if existing_request is not None or existing_promise is not None:
                    raise FinancialInputError("این پیام قبلاً ثبت مالی شده است.")
                money = Money(
                    int(state["amount_value"]),
                    CurrencyUnit(str(state["amount_unit"])),
                )
                if operation == "request_create":
                    holder = " ".join(str(state.get("account_holder") or "").split())
                    if state.get("request_manual_legacy"):
                        counterparty = session.scalar(
                            select(Counterparty).where(
                                Counterparty.group_id == group_id,
                                Counterparty.telegram_user_id == sender.id,
                                Counterparty.active.is_(True),
                            )
                        )
                        if counterparty is None:
                            counterparty = ledger.add_counterparty(
                                sender.display_name,
                                aliases=[sender.username] if sender.username else None,
                                telegram_user_id=sender.id,
                            )
                    else:
                        if not holder:
                            raise FinancialInputError(
                                "نام صاحب حساب باید پیش از ثبت نهایی بررسی شود."
                            )
                        if not any(
                            state.get(key)
                            for key in (
                                "destination_iban",
                                "destination_card",
                                "destination_account",
                            )
                        ):
                            raise FinancialInputError(
                                "حداقل یک شماره شبا، کارت یا حساب بررسی‌شده لازم است."
                            )
                        # Reviewed financial identity; never inferred from sender.
                        counterparty = ledger.add_counterparty(holder)
                    requested_at = source.occurred_at
                    if requested_at.tzinfo is None:
                        requested_at = requested_at.replace(tzinfo=UTC)
                    item = ledger.create_request(
                        counterparty_id=counterparty.id,
                        requested_at=requested_at,
                        money=money,
                        source_message_id=source.id,
                        actor_user_id=user_id,
                        reason=reason,
                        account_holder_name=holder or None,
                        destination_account=state.get("destination_account"),
                        destination_card=state.get("destination_card"),
                        destination_iban=state.get("destination_iban"),
                        destination_bank=state.get("destination_bank"),
                        source_parser_candidate=state.get("source_parser_candidate"),
                    )
                    result = (
                        f"درخواست #{item.id} برای {counterparty.display_name} ثبت شد و به پیام اصلی پیوند دارد."
                    )
                else:
                    counterparty = session.scalar(
                        select(Counterparty).where(
                            Counterparty.group_id == group_id,
                            Counterparty.telegram_user_id == sender.id,
                            Counterparty.active.is_(True),
                        )
                    )
                    if counterparty is None:
                        counterparty = ledger.add_counterparty(
                            sender.display_name,
                            aliases=[sender.username] if sender.username else None,
                            telegram_user_id=sender.id,
                        )
                    item = ledger.create_promise(
                        counterparty_id=counterparty.id,
                        source_message_id=source.id,
                        money=money,
                        actor_user_id=user_id,
                        reason=reason,
                    )
                    result = (
                        f"وعده پرداخت #{item.id} برای {counterparty.display_name} ثبت شد؛ "
                        "این مبلغ پرداخت محسوب نمی‌شود."
                    )
            elif operation == "payment_confirm":
                item = ledger.review_payment(
                    int(state["payment_id"]),
                    reviewer_user_id=user_id,
                    decision=ReviewDecision.CONFIRMED,
                    reason=reason,
                )
                result = f"رسید #{item.id} از نظر حسابداری تأیید شد. وصول بانکی همچنان مستقل است."
            elif operation == "payment_reject":
                item = ledger.review_payment(
                    int(state["payment_id"]),
                    reviewer_user_id=user_id,
                    decision=ReviewDecision.REJECTED,
                    reason=reason,
                )
                result = f"رسید #{item.id} رد شد و تخصیص‌های فعال آن قابل محاسبه نیست."
            elif operation == "payment_duplicate":
                item = ledger.review_payment(
                    int(state["payment_id"]),
                    reviewer_user_id=user_id,
                    decision=ReviewDecision.MARKED_DUPLICATE,
                    duplicate_of_payment_id=int(state["canonical_payment_id"]),
                    reason=reason,
                )
                result = f"رسید #{item.id} تکراری علامت خورد و دوباره شمرده نمی‌شود."
            elif operation == "payment_correct":
                money = Money(int(state["amount_value"]), CurrencyUnit(str(state["amount_unit"])))
                item = ledger.correct_payment(
                    int(state["payment_id"]),
                    reviewer_user_id=user_id,
                    money=money,
                    reason=reason,
                )
                result = f"مبلغ رسید #{item.id} اصلاح و نسخه قبلی در تاریخچه حفظ شد."
            elif operation == "payment_identity":
                item = ledger.assign_payment_counterparty(
                    int(state["payment_id"]),
                    counterparty_id=int(state["counterparty_id"]),
                    reviewer_user_id=user_id,
                    reason=reason,
                )
                party = session.scalar(
                    select(Counterparty).where(
                        Counterparty.id == item.counterparty_id,
                        Counterparty.group_id == group_id,
                    )
                )
                result = (
                    f"پرداخت‌کننده رسید #{item.id} به "
                    f"{party.display_name if party else 'شخص انتخاب‌شده'} تغییر یافت؛ "
                    "وضعیت تأیید سند تغییر نکرد."
                )
            elif operation == "payment_date_approve":
                item = ledger.approve_payment_date(
                    int(state["payment_id"]),
                    reviewer_user_id=user_id,
                    reason=reason,
                )
                result = (
                    f"استثنای تاریخ رسید #{item.id} تأیید شد؛ این تصمیم فقط مانع تاریخ را برمی‌دارد "
                    "و به معنی تأیید سند یا وصول بانکی نیست."
                )
            elif operation == "payment_date_reject":
                item = ledger.reject_payment_date(
                    int(state["payment_id"]),
                    reviewer_user_id=user_id,
                    reason=reason,
                )
                result = f"تاریخ رسید #{item.id} رد شد و مبلغ آن در جمع‌ها منظور نمی‌شود."
            elif operation == "payment_date_correct":
                item = ledger.correct_payment_date(
                    int(state["payment_id"]),
                    corrected_transaction_at=str(state["transaction_date"]),
                    reviewer_user_id=user_id,
                    reason=reason,
                )
                result = (
                    f"تاریخ رسید #{item.id} اصلاح شد؛ تاریخ استخراج‌شده اولیه در سوابق حفظ شده است."
                )
            elif operation == "payment_allocate":
                money = Money(int(state["amount_value"]), CurrencyUnit(str(state["amount_unit"])))
                if money.canonical_rial is None:
                    raise FinancialInputError("واحد تخصیص باید قابل تبدیل به ریال باشد.")
                item = ledger.allocate(
                    request_id=int(state["request_id"]),
                    payment_id=int(state["payment_id"]),
                    allocated_rial=money.canonical_rial,
                    actor_user_id=user_id,
                )
                result = f"تخصیص #{item.id} به‌صورت پیشنهادی ثبت شد و هنوز نیازمند تأیید است."
            elif operation == "allocation_confirm":
                item = ledger.confirm_allocation(
                    int(state["allocation_id"]),
                    reviewer_user_id=user_id,
                    reason=reason,
                )
                result = f"تخصیص #{item.id} تأیید شد."
            elif operation == "allocation_cancel":
                item = ledger.cancel_allocation(
                    int(state["allocation_id"]),
                    actor_user_id=user_id,
                    reason=reason,
                )
                result = f"تخصیص #{item.id} لغو شد؛ سوابق آن حفظ شده است."
            elif operation == "payment_settlement":
                item = ledger.confirm_external_settlement(
                    int(state["payment_id"]),
                    reviewer_user_id=user_id,
                    reason=reason,
                )
                result = f"وصول مستقل رسید #{item.id} ثبت شد."
            else:
                raise FinancialInputError("عملیات ناشناخته است.")
            interactions.consume(interaction)
            return ("✅ " + result).translate(_PERSIAN_DIGITS)

    @staticmethod
    def action_summary(state: dict[str, Any]) -> str:
        operation = str(state.get("operation"))
        names = {
            "payment_confirm": "تأیید حسابداری رسید",
            "payment_reject": "رد رسید",
            "payment_duplicate": "علامت‌گذاری رسید تکراری",
            "payment_correct": "اصلاح مبلغ رسید",
            "payment_identity": "تعیین پرداخت‌کننده رسید",
            "payment_allocate": "تخصیص رسید به درخواست",
            "allocation_confirm": "تأیید تخصیص",
            "allocation_cancel": "لغو تخصیص",
            "payment_settlement": "ثبت وصول مستقل",
            "payment_date_approve": "تأیید استثنای تاریخ رسید",
            "payment_date_reject": "رد تاریخ رسید",
            "payment_date_correct": "اصلاح تاریخ رسید",
            "request_create": "ثبت درخواست پرداخت از پیام اصلی",
            "promise_create": "ثبت وعده پرداخت از پیام اصلی",
        }
        lines = [f"تأیید نهایی: {names.get(operation, operation)}"]
        for key, label in (
            ("payment_id", "رسید"),
            ("request_id", "درخواست"),
            ("allocation_id", "تخصیص"),
            ("canonical_payment_id", "رسید اصلی"),
            ("counterparty_id", "شخص"),
        ):
            if key in state:
                lines.append(f"{label}: #{state[key]}")
        if "amount_value" in state:
            money = Money(int(state["amount_value"]), CurrencyUnit(str(state["amount_unit"])))
            lines.append(f"مبلغ: {toman(money.canonical_rial)}")
        if "transaction_date" in state:
            lines.append(f"تاریخ اصلاح‌شده: {state['transaction_date']}")
        if "source_sender_name" in state:
            lines.append(f"فرستنده پیام (منبع): {state['source_sender_name']}")
        if state.get("account_holder"):
            lines.append(f"صاحب حساب/طرف درخواست: {state['account_holder']}")
        identifier = next(
            (
                str(state[key])
                for key in ("destination_iban", "destination_card", "destination_account")
                if state.get(key)
            ),
            None,
        )
        if identifier:
            lines.append(f"مشخصات حساب: {FinancialWorkflowService._masked(identifier)}")
        lines.append(f"دلیل: {state.get('reason', '')}")
        lines.append("این عملیات سوابق قبلی را حذف نمی‌کند.")
        return "\n".join(lines).translate(_PERSIAN_DIGITS)

    def private_report_sources(self, group_id: int, period: Period) -> str:
        start, end = period.utc_bounds
        with self.database.session() as session:
            report = ReportingEngine(session, group_id).build(period_start=start, period_end=end)
            group = self._group(session, group_id)
            lines = ["منابع گزارش مجاز"]
            for item in report.request_lines:
                if item.source:
                    lines.append(
                        f"• درخواست #{item.request_id}: "
                        f"{source_link(group.telegram_chat_id, item.source.telegram_message_id)}"
                    )
                request = session.scalar(
                    select(PaymentRequest).where(
                        PaymentRequest.id == item.request_id,
                        PaymentRequest.group_id == group_id,
                    )
                )
                if request is not None:
                    lines.append(
                        f"  صاحب حساب بررسی‌شده: {request.account_holder_name or 'ثبت نشده'}"
                    )
                    for label, value in (
                        ("شبا", request.destination_iban),
                        ("کارت", request.destination_card),
                        ("حساب", request.destination_account),
                        ("بانک", request.destination_bank),
                    ):
                        if value:
                            lines.append(f"  {label}: {value}")
                for payment_id in item.payment_ids:
                    payment = session.scalar(
                        select(Payment).where(
                            Payment.id == payment_id,
                            Payment.group_id == group_id,
                        )
                    )
                    message = (
                        session.get(TelegramMessage, payment.source_message_id)
                        if payment and payment.source_message_id
                        else None
                    )
                    if message is not None:
                        lines.append(
                            f"  ↳ رسید #{payment_id}: "
                            f"{source_link(group.telegram_chat_id, message.telegram_message_id)}"
                        )
            for item in report.unallocated_payments:
                if item.source:
                    lines.append(
                        f"• رسید #{item.payment_id}: "
                        f"{source_link(group.telegram_chat_id, item.source.telegram_message_id)}"
                    )
            if len(lines) == 1:
                lines.append("منبعی برای این بازه وجود ندارد.")
            return "\n".join(lines)

    @staticmethod
    def _period_label(period: Period) -> str:
        if period.days == 1:
            value = period.start_date.strftime("%Y/%m/%d")
        else:
            value = f"{period.start_date:%Y/%m/%d} تا {(period.end_date - timedelta(days=1)):%Y/%m/%d}"
        return value.translate(_PERSIAN_DIGITS)

    @staticmethod
    def _page(session, query, page: int):
        page = max(page, 0)
        rows = list(session.scalars(query.offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1)))
        return rows[:PAGE_SIZE], page > 0, len(rows) > PAGE_SIZE

    @staticmethod
    def _latest_attempt(session, evidence_id: int) -> ExtractionAttempt | None:
        return session.scalar(
            select(ExtractionAttempt)
            .where(ExtractionAttempt.evidence_id == evidence_id)
            .order_by(ExtractionAttempt.attempt_number.desc())
            .limit(1)
        )

    @staticmethod
    def _processing_counts(
        session, group_id: int, start: datetime, end: datetime
    ) -> ReceiptProcessingCounts:
        statuses: list[str] = []
        statuses.extend(
            session.scalars(
                select(ReceiptJob.status)
                .join(TelegramMessage, TelegramMessage.id == ReceiptJob.source_message_id)
                .where(
                    ReceiptJob.group_id == group_id,
                    TelegramMessage.group_id == group_id,
                    TelegramMessage.occurred_at >= start,
                    TelegramMessage.occurred_at < end,
                    ReceiptJob.status.in_(_UNFINISHED_JOB_STATES),
                )
            )
        )
        statuses.extend(
            session.scalars(
                select(TextReceiptJob.status)
                .join(TelegramMessage, TelegramMessage.id == TextReceiptJob.source_message_id)
                .where(
                    TextReceiptJob.group_id == group_id,
                    TelegramMessage.group_id == group_id,
                    TelegramMessage.occurred_at >= start,
                    TelegramMessage.occurred_at < end,
                    TextReceiptJob.status.in_(_UNFINISHED_JOB_STATES),
                )
            )
        )
        return ReceiptProcessingCounts(
            queued=sum(item in {"queued_download", "queued_extraction"} for item in statuses),
            processing=sum(item in {"downloading", "extracting"} for item in statuses),
            retrying=sum(item.endswith("_failed_retryable") for item in statuses),
            failed=sum(item == "failed_final" for item in statuses),
        )

    @staticmethod
    def _allocated_for_payment(session, group_id: int, payment_id: int) -> int:
        return int(
            session.scalar(
                select(func.coalesce(func.sum(RequestPaymentAllocation.allocated_rial), 0)).where(
                    RequestPaymentAllocation.group_id == group_id,
                    RequestPaymentAllocation.payment_id == payment_id,
                    RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
                )
            )
            or 0
        )

    @staticmethod
    def _allocated_for_request(session, group_id: int, request_id: int) -> int:
        return int(
            session.scalar(
                select(func.coalesce(func.sum(RequestPaymentAllocation.allocated_rial), 0)).where(
                    RequestPaymentAllocation.group_id == group_id,
                    RequestPaymentAllocation.request_id == request_id,
                    RequestPaymentAllocation.state.in_(ACTIVE_ALLOCATION_STATES),
                )
            )
            or 0
        )
