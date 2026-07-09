import re

from app.errors import ErrorCode, PatentServiceError
from app.models.patents import PatentReference, PatentSource

_SEPARATOR_PATTERN = re.compile(r"[\s./-]+")
_EP_PATTERN = re.compile(r"^EP(?P<doc_number>\d{4,12})(?P<kind>[A-Z]\d{1,2})?$")
_WO_COMPACT_PATTERN = re.compile(
    r"^WO(?P<year>\d{4})(?P<serial>\d{6})(?P<kind>[A-Z]\d{1,2})?$"
)
_WO_SLASH_PATTERN = re.compile(
    r"^WO/(?P<year>\d{4})/(?P<serial>\d{6})(?P<kind>[A-Z]\d{1,2})?$"
)


def normalize_patent_number(raw_value: str) -> PatentReference:
    value = raw_value.strip().upper()
    if not value:
        raise PatentServiceError(
            code=ErrorCode.INVALID_PATENT_NUMBER_FORMAT,
            status_code=422,
            message="Patent number must not be empty.",
        )

    compact = _SEPARATOR_PATTERN.sub("", value)
    if compact.startswith("EP"):
        return _normalize_ep(compact)
    if compact.startswith("WO") or value.startswith("WO/"):
        return _normalize_wo(value, compact)

    prefix_match = re.match(r"^(?P<prefix>[A-Z]{2})", compact)
    if prefix_match:
        raise PatentServiceError(
            code=ErrorCode.UNSUPPORTED_JURISDICTION,
            status_code=422,
            message="Patent number jurisdiction is not supported.",
            details={"prefix": prefix_match.group("prefix")},
        )

    raise PatentServiceError(
        code=ErrorCode.INVALID_PATENT_NUMBER_FORMAT,
        status_code=422,
        message="Patent number format is invalid.",
    )


def _normalize_ep(compact: str) -> PatentReference:
    match = _EP_PATTERN.fullmatch(compact)
    if not match:
        raise PatentServiceError(
            code=ErrorCode.INVALID_PATENT_NUMBER_FORMAT,
            status_code=422,
            message="EP publication number format is invalid.",
        )

    doc_number = match.group("doc_number")
    kind_code = match.group("kind")
    normalized_number = f"EP{doc_number}{kind_code or ''}"
    lookup_number = f"EP{doc_number}.{kind_code}" if kind_code else f"EP{doc_number}"
    return PatentReference(
        source=PatentSource.EPO,
        normalized_number=normalized_number,
        display_number=normalized_number,
        country_code="EP",
        doc_number=doc_number,
        kind_code=kind_code,
        lookup_number=lookup_number,
    )


def _normalize_wo(value: str, compact: str) -> PatentReference:
    slash_match = _WO_SLASH_PATTERN.fullmatch(value.replace(" ", ""))
    compact_match = _WO_COMPACT_PATTERN.fullmatch(compact)
    match = slash_match or compact_match
    if not match:
        raise PatentServiceError(
            code=ErrorCode.INVALID_PATENT_NUMBER_FORMAT,
            status_code=422,
            message="WO publication number format is invalid.",
        )

    year = match.group("year")
    serial = match.group("serial")
    kind_code = match.group("kind")
    normalized_number = f"WO{year}{serial}{kind_code or ''}"
    return PatentReference(
        source=PatentSource.WIPO,
        normalized_number=normalized_number,
        display_number=f"WO/{year}/{serial}",
        country_code="WO",
        doc_number=f"{year}{serial}",
        kind_code=kind_code,
        lookup_number=normalized_number,
    )
