import base64
import hashlib
import hmac
import json
import time
from typing import Any, Literal

from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentAnalysisResponse,
    PatentLookupApiResponse,
    PatentLookupEpResponse,
    PatentLookupResponse,
)

ReceiptPurpose = Literal["lookup", "analysis"]


class ReceiptSigner:
    def __init__(self, settings: Settings) -> None:
        self._secret = (settings.api_key or "").encode("utf-8")
        self._ttl_seconds = settings.receipt_ttl_seconds

    @property
    def configured(self) -> bool:
        return bool(self._secret)

    def sign_lookup(self, response: PatentLookupApiResponse) -> str | None:
        if not self.configured:
            return None
        return self._sign(
            "lookup",
            response.model_dump(
                mode="json", exclude={"lookup_receipt"}, exclude_none=False
            ),
        )

    def sign_analysis(self, response: PatentAnalysisResponse) -> str | None:
        if not self.configured:
            return None
        return self._sign(
            "analysis",
            response.model_dump(
                mode="json", exclude={"analysis_receipt"}, exclude_none=False
            ),
        )

    def verify_lookup(self, receipt: str) -> PatentLookupApiResponse:
        payload = self._verify(receipt, "lookup")
        data = payload["data"]
        try:
            if data.get("source") == "epo":
                return PatentLookupEpResponse.model_validate(data)
            return PatentLookupResponse.model_validate(data)
        except (AttributeError, ValueError, TypeError) as exc:
            raise _invalid_receipt("The lookup receipt payload is invalid.") from exc

    def verify_analysis(self, receipt: str) -> PatentAnalysisResponse:
        payload = self._verify(receipt, "analysis")
        try:
            return PatentAnalysisResponse.model_validate(payload["data"])
        except (ValueError, TypeError) as exc:
            raise _invalid_receipt("The analysis receipt payload is invalid.") from exc

    def _sign(self, purpose: ReceiptPurpose, data: dict[str, Any]) -> str:
        payload = {
            "purpose": purpose,
            "issued_at": int(time.time()),
            "data": data,
        }
        encoded = _encode(
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        )
        signature = _encode(
            hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{encoded}.{signature}"

    def _verify(
        self, receipt: str, expected_purpose: ReceiptPurpose
    ) -> dict[str, Any]:
        if not self.configured:
            raise PatentServiceError(
                code=ErrorCode.SERVICE_AUTH_REQUIRED,
                status_code=503,
                message="Patent receipt signing is not configured.",
                source="service",
            )
        try:
            encoded, signature = receipt.split(".", 1)
        except ValueError as exc:
            raise _invalid_receipt("The patent receipt format is invalid.") from exc
        expected = _encode(
            hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            raise _invalid_receipt("The patent receipt signature is invalid.")
        try:
            payload = json.loads(_decode(encoded))
        except (ValueError, UnicodeDecodeError) as exc:
            raise _invalid_receipt("The patent receipt payload is invalid.") from exc
        if payload.get("purpose") != expected_purpose:
            raise _invalid_receipt("The patent receipt purpose is invalid.")
        issued_at = payload.get("issued_at")
        if not isinstance(issued_at, int):
            raise _invalid_receipt("The patent receipt timestamp is invalid.")
        now = int(time.time())
        if issued_at > now + 60 or now - issued_at > self._ttl_seconds:
            raise _invalid_receipt("The patent receipt has expired.")
        if not isinstance(payload.get("data"), dict):
            raise _invalid_receipt("The patent receipt data is invalid.")
        return payload


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode("utf-8")


def _invalid_receipt(message: str) -> PatentServiceError:
    return PatentServiceError(
        code=ErrorCode.INVALID_RECEIPT,
        status_code=422,
        message=message,
        source="service",
    )
