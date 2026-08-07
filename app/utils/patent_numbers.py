import re

from app.errors import ErrorCode, PatentServiceError
from app.models.patents import PatentReference, PatentSource

_SEPARATOR_PATTERN = re.compile(r"[\s./-]+")
_EP_PATTERN = re.compile(r"^EP(?P<doc_number>\d{4,12})(?P<kind>[A-Z]\d{1,2})?$")
_EP_APPLICATION_WITH_CHECK_PATTERN = re.compile(
    r"^EP(?P<doc_number>\d{8})\.(?P<check_digit>\d)$"
)
_EP_APPLICATION_EPODOC_PATTERN = re.compile(r"^EP(?P<doc_number>\d{8})$")
_WO_COMPACT_PATTERN = re.compile(
    r"^WO(?P<year>\d{4})(?P<serial>\d{6})(?P<kind>[A-Z]\d{1,2})?$"
)
_WO_SLASH_PATTERN = re.compile(
    r"^WO/(?P<year>\d{4})/(?P<serial>\d{6})(?P<kind>[A-Z]\d{1,2})?$"
)
_PCT_COMPACT_PATTERN = re.compile(
    r"^PCT(?P<office>[A-Z]{2})(?P<year>\d{4})(?P<serial>\d{6})$"
)
_PCT_SLASH_PATTERN = re.compile(
    r"^PCT/(?P<office>[A-Z]{2})(?P<year>\d{4})/(?P<serial>\d{6})$"
)
_NATIONAL_PATTERN = re.compile(
    r"^(?P<country>[A-Z]{2})(?P<doc_number>[A-Z0-9]{4,}?)(?P<kind>[A-Z]\d{0,2})?$"
)
_US_PUBLICATION_PATTERN = re.compile(
    r"^(?P<year>\d{4})(?P<serial>\d{7})$"
)


def normalize_patent_number(
    raw_value: str,
    *,
    source_override: PatentSource | str | None = None,
) -> PatentReference:
    value = raw_value.strip().upper()
    if not value:
        raise PatentServiceError(
            code=ErrorCode.INVALID_PATENT_NUMBER_FORMAT,
            status_code=422,
            message="Patent number must not be empty.",
        )

    ep_value = re.sub(r"[\s/-]+", "", value)
    if ep_value.startswith("EP"):
        application_match = _EP_APPLICATION_WITH_CHECK_PATTERN.fullmatch(ep_value)
        if application_match:
            return _normalize_ep_application(application_match)

    compact = _SEPARATOR_PATTERN.sub("", value)
    override = PatentSource(source_override) if source_override else None
    if override is PatentSource.EPO and not compact.startswith(("EP", "WO", "PCT")):
        return _normalize_national_epo(value, compact)
    if compact.startswith("PCT"):
        reference = _normalize_pct(value, compact)
        return _require_source(reference, override)
    if compact.startswith("EP"):
        application_match = _EP_APPLICATION_EPODOC_PATTERN.fullmatch(compact)
        if application_match:
            reference = _normalize_ep_application(application_match)
        else:
            reference = _normalize_ep(compact)
        return _require_source(reference, override)
    if compact.startswith("WO") or value.startswith("WO/"):
        reference = _normalize_wo(value, compact)
        return _require_source(reference, override)

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


def _require_source(
    reference: PatentReference, override: PatentSource | None
) -> PatentReference:
    if override is None or reference.source is override:
        return reference
    raise PatentServiceError(
        code=ErrorCode.UNSUPPORTED_JURISDICTION,
        status_code=422,
        message="The patent number is incompatible with the requested source.",
        source=override.value,
        details={
            "requested_source": override.value,
            "detected_source": reference.source.value,
        },
    )


def _normalize_national_epo(value: str, compact: str) -> PatentReference:
    match = _NATIONAL_PATTERN.fullmatch(compact)
    if not match:
        raise PatentServiceError(
            code=ErrorCode.INVALID_PATENT_NUMBER_FORMAT,
            status_code=422,
            message="National publication number format is invalid.",
            source="epo",
        )

    country_code = match.group("country")
    input_doc_number = match.group("doc_number")
    kind_code = match.group("kind") or None
    doc_number = _to_epodoc_document_number(country_code, input_doc_number)
    normalized_number = f"{country_code}{doc_number}{kind_code or ''}"
    return PatentReference(
        source=PatentSource.EPO,
        normalized_number=normalized_number,
        display_number=value,
        country_code=country_code,
        doc_number=doc_number,
        kind_code=kind_code,
        lookup_number=f"{country_code}{doc_number}",
    )


def _to_epodoc_document_number(country_code: str, doc_number: str) -> str:
    if country_code != "US":
        return doc_number
    match = _US_PUBLICATION_PATTERN.fullmatch(doc_number)
    if not match:
        return doc_number
    serial = match.group("serial")
    return f"{match.group('year')}{serial[1:] if serial.startswith('0') else serial}"


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


def _normalize_ep_application(match: re.Match[str]) -> PatentReference:
    doc_number = match.group("doc_number")
    check_digit = match.groupdict().get("check_digit")
    normalized_number = (
        f"EP{doc_number}.{check_digit}" if check_digit else f"EP{doc_number}"
    )
    return PatentReference(
        source=PatentSource.EPO,
        normalized_number=normalized_number,
        display_number=normalized_number,
        country_code="EP",
        doc_number=doc_number,
        lookup_number=f"EP{doc_number}",
        reference_type="application",
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


def _normalize_pct(value: str, compact: str) -> PatentReference:
    slash_match = _PCT_SLASH_PATTERN.fullmatch(value.replace(" ", ""))
    compact_match = _PCT_COMPACT_PATTERN.fullmatch(compact)
    match = slash_match or compact_match
    if not match:
        raise PatentServiceError(
            code=ErrorCode.INVALID_PATENT_NUMBER_FORMAT,
            status_code=422,
            message="PCT international application number format is invalid.",
        )

    office = match.group("office")
    year = match.group("year")
    serial = match.group("serial")
    rest_number = f"{office}{year}{serial}"
    return PatentReference(
        source=PatentSource.WIPO,
        normalized_number=f"PCT{rest_number}",
        display_number=f"PCT/{office}{year}/{serial}",
        country_code="PCT",
        doc_number=rest_number,
        lookup_number=rest_number,
    )
