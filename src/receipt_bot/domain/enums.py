from enum import StrEnum


class CurrencyUnit(StrEnum):
    RIAL = "rial"
    TOMAN = "toman"
    UNKNOWN = "unknown"


class MembershipRole(StrEnum):
    MEMBER = "member"
    ACCOUNTANT = "accountant"


class RequestState(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    PARTIALLY_DOCUMENTED = "partially_documented"
    DOCUMENTED = "documented"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"


class StorageState(StrEnum):
    PRESENT = "present"
    DEDUPLICATED = "deduplicated"
    DELETED = "deleted"
    MISSING = "missing"
    DELETE_FAILED = "delete_failed"


class ExtractionState(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"


class TelegramUpdateState(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED_RETRYABLE = "failed_retryable"


class ReceiptJobState(StrEnum):
    QUEUED_DOWNLOAD = "queued_download"
    DOWNLOADING = "downloading"
    DOWNLOAD_FAILED_RETRYABLE = "download_failed_retryable"
    QUEUED_EXTRACTION = "queued_extraction"
    EXTRACTING = "extracting"
    EXTRACTION_FAILED_RETRYABLE = "extraction_failed_retryable"
    SUCCEEDED = "succeeded"
    DUPLICATE_EXACT = "duplicate_exact"
    FAILED_FINAL = "failed_final"


class EvidenceKind(StrEnum):
    IMAGE = "image"
    TELEGRAM_TEXT = "telegram_text"


class PaymentEvidenceState(StrEnum):
    SUBMITTED = "submitted"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    CONFIRMED = "confirmed"


class SettlementState(StrEnum):
    NOT_CHECKED = "not_checked"
    EXTERNALLY_CONFIRMED = "externally_confirmed"


class PromiseState(StrEnum):
    OPEN = "open"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"


class ReviewDecision(StrEnum):
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    REJECTED = "rejected"
    MARKED_DUPLICATE = "marked_duplicate"
    REOPENED = "reopened"


class AllocationState(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class MatchDisposition(StrEnum):
    NONE = "none"
    UNIQUE = "unique"
    AMBIGUOUS = "ambiguous"
