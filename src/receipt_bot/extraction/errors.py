from __future__ import annotations


class ExtractionError(Exception):
    """Safe base exception; messages must never include credentials or receipt content."""

    retryable = False
    code = "extraction_error"


class ExtractionConfigurationError(ExtractionError):
    code = "configuration_error"


class InvalidImageError(ExtractionError):
    code = "invalid_image"


class UnsupportedImageError(InvalidImageError):
    code = "unsupported_image"


class ImageLimitError(InvalidImageError):
    code = "image_limit"


class ProviderError(ExtractionError):
    code = "provider_error"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderAuthenticationError(ProviderError):
    code = "provider_authentication"


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limit"
    retryable = True


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    retryable = True


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"
    retryable = True


class ProviderModelUnavailableError(ProviderError):
    code = "provider_model_unavailable"


class ProviderRefusalError(ProviderError):
    code = "provider_refusal"


class MalformedProviderResponseError(ProviderError):
    code = "malformed_provider_response"
    retryable = True
