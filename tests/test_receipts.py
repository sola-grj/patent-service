import pytest

from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentAnalysisAggregate,
    PatentAnalysisResponse,
    PatentLookupEpResponse,
    PatentSource,
)
from app.security.receipts import ReceiptSigner


def test_receipts_round_trip_without_embedding_receipt_fields():
    signer = ReceiptSigner(Settings(api_key="test-service-secret"))
    lookup = PatentLookupEpResponse(
        source=PatentSource.EPO,
        normalized_number="EP1234567A1",
        display_number="EP1234567A1",
        title="Example",
    )
    analysis = PatentAnalysisResponse(
        input_mode="patent_number",
        status="success",
        patent_number="EP1234567A1",
        aggregate=PatentAnalysisAggregate(total_words=123),
    )

    verified_lookup = signer.verify_lookup(signer.sign_lookup(lookup))
    verified_analysis = signer.verify_analysis(signer.sign_analysis(analysis))

    assert verified_lookup.normalized_number == "EP1234567A1"
    assert verified_lookup.lookup_receipt is None
    assert verified_analysis.aggregate.total_words == 123
    assert verified_analysis.analysis_receipt is None


def test_tampered_receipt_is_rejected():
    signer = ReceiptSigner(Settings(api_key="test-service-secret"))
    analysis = PatentAnalysisResponse(
        input_mode="patent_number",
        status="success",
        patent_number="EP1234567A1",
    )
    receipt = signer.sign_analysis(analysis)

    with pytest.raises(PatentServiceError) as excinfo:
        signer.verify_analysis(f"{receipt}tampered")

    assert excinfo.value.code == ErrorCode.INVALID_RECEIPT
