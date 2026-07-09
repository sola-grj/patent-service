from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_PATENT_NUMBER_FORMAT = "invalid_patent_number_format"
    UNSUPPORTED_JURISDICTION = "unsupported_jurisdiction"
    SOURCE_AUTH_REQUIRED = "source_auth_required"
    SOURCE_ACCESS_NOT_CONFIGURED = "source_access_not_configured"
    SOURCE_NO_RESULT = "source_no_result"
    SOURCE_RATE_LIMIT = "source_rate_limit"
    SOURCE_UNAVAILABLE = "source_unavailable"
    ORIGINAL_FILE_NOT_AVAILABLE = "original_file_not_available"
    UPSTREAM_RESPONSE_INVALID = "upstream_response_invalid"


class PatentServiceError(Exception):
    def __init__(
        self,
        *,
        code: ErrorCode,
        status_code: int,
        message: str,
        source: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.source = source
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "source": self.source,
                "details": self.details,
            }
        }
