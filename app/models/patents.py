from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PatentSource(StrEnum):
    EPO = "epo"
    WIPO = "wipo"


class PatentReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: PatentSource
    normalized_number: str
    display_number: str
    country_code: str
    doc_number: str
    kind_code: str | None = None
    lookup_number: str
    reference_type: Literal["publication", "application"] = "publication"


class PatentLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patent_number: str
    include_original_file: bool = False


class PatentLookupCacheInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_cached: bool = False
    reason: Literal["official_source_no_result"] | None = None
    last_successful_fetch_at: str | None = None


class PatentRepresentative(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    organization: str = ""
    address: str = ""
    country: str = ""


class PatentPriorityData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str = ""
    date: str = ""
    country: str = ""
    kind: str = ""


class PatentDesignatedStates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regions: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    protection_types: list[str] = Field(default_factory=list)


class PatentBasicInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    abstract: str = ""
    publication_date: str = ""
    application_number: str = ""
    applicants: list[str] = Field(default_factory=list)
    inventors: list[str] = Field(default_factory=list)
    representatives: list[PatentRepresentative] = Field(default_factory=list)
    ipc: list[str] = Field(default_factory=list)
    cpc: list[str] = Field(default_factory=list)


class PatentOriginalFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool = False
    content_type: str = ""
    filename: str = ""
    download_url: str = ""
    storage_path: str = ""


class PatentLookupWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    field: str
    message: str
    source: str


class PatentLookupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: PatentSource
    normalized_number: str
    display_number: str
    data_origin: Literal["official", "cache_fallback"] = "official"
    cache: PatentLookupCacheInfo = Field(default_factory=PatentLookupCacheInfo)
    lookup_receipt: str | None = None
    basic_info: PatentBasicInfo
    application_date: str | None = None
    application_no: str | None = None
    publication_date: str | None = None
    publication_no: str | None = None
    agents: list[PatentRepresentative] = Field(default_factory=list)
    priority_data: list[PatentPriorityData] = Field(default_factory=list)
    publication_language: str | None = None
    filing_language: str | None = None
    designated_states: PatentDesignatedStates = Field(
        default_factory=PatentDesignatedStates
    )
    related_patent_documents: list[str] = Field(default_factory=list)
    abstract_words: int | None = None
    description_words: int | None = None
    claims_count: int | None = None
    claims_words: int | None = None
    drawings: "PatentDrawingsInfo" = Field(default_factory=lambda: PatentDrawingsInfo())
    original_file: PatentOriginalFile
    warnings: list[PatentLookupWarning] = Field(default_factory=list)
    raw_source_refs: dict[str, Any] = Field(default_factory=dict)


class PatentDrawingsInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_drawings: bool = False
    drawing_page_count: int | None = None
    drawing_labels: list[str] = Field(default_factory=list)


class PatentLookupEpResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: PatentSource
    normalized_number: str
    display_number: str
    data_origin: Literal["official", "cache_fallback"] = "official"
    cache: PatentLookupCacheInfo = Field(default_factory=PatentLookupCacheInfo)
    lookup_receipt: str | None = None
    title: str = ""
    abstract: str = ""
    ipc: list[str] = Field(default_factory=list)
    cpc: list[str] = Field(default_factory=list)
    applicants: list[str] = Field(default_factory=list)
    inventors: list[str] = Field(default_factory=list)
    representatives: list[PatentRepresentative] = Field(default_factory=list)
    agents: list[PatentRepresentative] = Field(default_factory=list)
    priority_data: list[PatentPriorityData] = Field(default_factory=list)
    publication_language: str | None = None
    filing_language: str | None = None
    designated_states: PatentDesignatedStates = Field(
        default_factory=PatentDesignatedStates
    )
    related_patent_documents: list[str] = Field(default_factory=list)
    language: str | None = None
    first_priority_date: str | None = None
    international_filing_date: str | None = None
    filing_deadline_30_months: str | None = None
    filing_deadline_31_months: str | None = None
    application_date: str | None = None
    application_no: str | None = None
    publication_date: str | None = None
    publication_no: str | None = None
    abstract_words: int | None = None
    description_words: int | None = None
    claims_count: int | None = None
    claims_words: int | None = None
    total_pages: int | None = None
    drawings: PatentDrawingsInfo = Field(default_factory=PatentDrawingsInfo)
    original_file_download_url: str | None = None
    warnings: list[PatentLookupWarning] = Field(default_factory=list)
    raw_source_refs: dict[str, Any] = Field(default_factory=dict)


PatentLookupApiResponse = PatentLookupEpResponse | PatentLookupResponse


class PatentAnalysisWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    filename: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class PatentPartAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    word_count: int = 0
    status: Literal["found", "missing", "unclassified", "error"] = "missing"
    method: str = "none"
    confidence: Literal["high", "medium", "low", "none"] = "none"


class PatentFivePartAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    abstract: PatentPartAnalysis = Field(default_factory=PatentPartAnalysis)
    abstract_drawing: PatentPartAnalysis = Field(default_factory=PatentPartAnalysis)
    description: PatentPartAnalysis = Field(default_factory=PatentPartAnalysis)
    description_drawings: PatentPartAnalysis = Field(default_factory=PatentPartAnalysis)
    claims: PatentPartAnalysis = Field(default_factory=PatentPartAnalysis)
    unclassified: PatentPartAnalysis = Field(default_factory=PatentPartAnalysis)


class PatentFileAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    file_type: Literal["pdf", "doc", "docx", "wipo_zip", "epo_zip"]
    sha256: str = ""
    status: Literal["success", "partial", "failed"] = "success"
    parts: PatentFivePartAnalysis = Field(default_factory=PatentFivePartAnalysis)
    document_text_words: int = 0
    drawing_ocr_words: int = 0
    total_words: int = 0
    warnings: list[PatentAnalysisWarning] = Field(default_factory=list)


class PatentAnalysisAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    abstract_words: int = 0
    abstract_drawing_words: int = 0
    description_words: int = 0
    description_drawings_words: int = 0
    claims_words: int = 0
    unclassified_words: int = 0
    total_words: int = 0


class PatentAnalysisArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    filename: str
    mime_type: str
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: str


class PatentAnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_mode: Literal["upload", "patent_number"]
    status: Literal["success", "partial", "failed"]
    patent_number: str | None = None
    analysis_receipt: str | None = None
    artifact: PatentAnalysisArtifact | None = None
    counting_standard: str = (
        "Unicode words including numeric tokens; CJK characters counted individually"
    )
    excluded_content: list[str] = Field(
        default_factory=lambda: [
            "bibliographic cover fields outside the abstract",
            "repeated patent-number headers, footers and page numbers",
            "search reports and other procedural documents",
        ]
    )
    files: list[PatentFileAnalysis] = Field(default_factory=list)
    aggregate: PatentAnalysisAggregate = Field(default_factory=PatentAnalysisAggregate)
    warnings: list[PatentAnalysisWarning] = Field(default_factory=list)


class PatentReceiptVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookup_receipt: str
    analysis_receipt: str


class PatentReceiptVerificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookup: PatentLookupEpResponse | PatentLookupResponse
    analysis: PatentAnalysisResponse


class PatentCacheRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    lookup_receipt: str
    analysis_receipt: str


class PatentCacheAcceptedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    patent_id: str
    status: Literal["pending", "processing", "completed", "failed"]
