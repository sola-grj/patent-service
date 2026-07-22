from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_PATENT_NUMBER_FORMAT = "invalid_patent_number_format"
    UNSUPPORTED_JURISDICTION = "unsupported_jurisdiction"
    SOURCE_AUTH_REQUIRED = "source_auth_required"
    SOURCE_ACCESS_DENIED = "source_access_denied"
    SOURCE_ACCESS_NOT_CONFIGURED = "source_access_not_configured"
    SOURCE_NO_RESULT = "source_no_result"
    SOURCE_RATE_LIMIT = "source_rate_limit"
    SOURCE_UNAVAILABLE = "source_unavailable"
    ORIGINAL_FILE_NOT_AVAILABLE = "original_file_not_available"
    UPSTREAM_RESPONSE_INVALID = "upstream_response_invalid"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    FILE_SIGNATURE_MISMATCH = "file_signature_mismatch"
    UPLOAD_TOO_LARGE = "upload_too_large"
    AMBIGUOUS_ANALYSIS_INPUT = "ambiguous_analysis_input"
    DOCUMENT_CONVERSION_UNAVAILABLE = "document_conversion_unavailable"
    DOCUMENT_CONVERSION_FAILED = "document_conversion_failed"
    DOCUMENT_PARSE_FAILED = "document_parse_failed"
    SECTION_DETECTION_INCOMPLETE = "section_detection_incomplete"
    OCR_FAILED = "ocr_failed"
    ANALYSIS_TIMEOUT = "analysis_timeout"


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
