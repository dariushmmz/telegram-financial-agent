from __future__ import annotations

from receipt_bot.domain.money import format_rial_as_toman
from receipt_bot.domain.time import to_tehran
from receipt_bot.reporting.models import ReportProjection


_STATUS = {
    "complete": "تکمیل",
    "awaiting_verification": "در انتظار تأیید حسابداری",
    "partial": "ناقص / نیازمند بررسی",
    "over_documented": "مبلغ مستند بیش از درخواست",
    "needs_review": "اطلاعات ناکافی / نیازمند بررسی",
    "cancelled": "لغوشده",
}
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _toman(rial: int | None) -> str:
    if rial is None:
        return "اطلاعات ناکافی / نیازمند بررسی"
    return f"{format_rial_as_toman(rial)} تومان"


def render_persian_report(report: ReportProjection) -> str:
    start = to_tehran(report.period_start)
    end = to_tehran(report.period_end)
    heading = (
        f"گزارش حساب‌ها — مبالغ به تومان\n"
        f"بازه: {start:%Y/%m/%d %H:%M} تا {end:%Y/%m/%d %H:%M}"
    ).translate(_PERSIAN_DIGITS)
    lines = [heading, ""]
    if not report.request_lines:
        lines.append("در این بازه درخواست پرداختی ثبت نشده است.")
    for index, item in enumerate(report.request_lines, start=1):
        difference = _toman(item.documented_difference_rial)
        line = (
            f"{index}. {item.counterparty_name}: درخواست {_toman(item.requested_rial)}؛ "
            f"مبلغ مستند {_toman(item.submitted_rial)}؛ "
            f"رسید تأیید حسابداری‌شده {_toman(item.evidence_confirmed_rial)}؛ "
            f"تخصیص تأییدشده {_toman(item.confirmed_rial)}؛ "
            f"در انتظار تأیید تخصیص {_toman(item.unverified_rial)}؛ "
            f"اختلاف {difference}؛ وضعیت: {_STATUS[item.status]}."
        )
        lines.append(line.translate(_PERSIAN_DIGITS))
        if item.warnings:
            lines.append("   ⚠️ اطلاعات این ردیف نیازمند بررسی است.")

    if report.unallocated_payments:
        lines.extend(["", "رسیدهای تخصیص‌نیافته:"])
        for item in report.unallocated_payments:
            if item.counterparty_name:
                identity = item.counterparty_name
            elif item.source and item.source.sender_display_name:
                identity = (
                    f"فرستنده تلگرام: {item.source.sender_display_name} "
                    "(پرداخت‌کننده تعیین نشده)"
                )
            else:
                identity = "پرداخت‌کننده تعیین نشده"
            if item.evidence_state == "confirmed":
                state = "تأیید حسابداری شده؛ هنوز به درخواست متصل نشده"
            elif item.evidence_state == "needs_review":
                state = "نیازمند بررسی حسابداری و اتصال به درخواست"
            else:
                state = "تأیید حسابداری نشده؛ هنوز به درخواست متصل نشده"
            lines.append(
                f"• {identity}: {_toman(item.unallocated_rial)} — {state}."
            )

    if report.promises:
        lines.extend(["", "وعده‌های پرداخت (خارج از جمع پرداخت‌ها):"])
        for item in report.promises:
            lines.append(f"• {item.counterparty_name}: {_toman(item.amount_rial)}؛ وضعیت {item.state}.")

    totals = report.totals
    lines.extend(
        [
            "",
            f"جمع درخواست‌های قابل مقایسه: {_toman(totals.requested_rial)}",
            f"جمع مبالغ مستند: {_toman(totals.submitted_rial)}",
            f"جمع رسیدهای تأیید حسابداری‌شده: {_toman(totals.evidence_confirmed_rial)}",
            f"جمع تخصیص‌های تأییدشده به درخواست: {_toman(totals.confirmed_rial)}",
            f"جمع تأییدنشده از نظر اتصال/تخصیص: {_toman(totals.unverified_rial)}",
            f"اختلاف قابل مقایسه: {_toman(totals.documented_difference_rial)}",
        ]
    )
    if totals.unresolved_count:
        lines.append(
            f"⚠️ {str(totals.unresolved_count).translate(_PERSIAN_DIGITS)} مورد حل‌نشده یا مستثنا وجود دارد."
        )
    lines.extend(
        [
            "",
            "مبالغ رسیدها به معنی تأیید مستقل وصول بانکی نیستند. اختلاف‌ها نیز تا زمان بررسی حسابداری بدهی قطعی محسوب نمی‌شوند.",
        ]
    )
    return "\n".join(lines)
