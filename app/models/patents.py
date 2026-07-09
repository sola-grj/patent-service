from enum import StrEnum
from typing import Any

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


class PatentLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patent_number: str
    include_original_file: bool = False


class PatentBasicInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    abstract: str = ""
    publication_date: str = ""
    application_number: str = ""
    applicants: list[str] = Field(default_factory=list)
    inventors: list[str] = Field(default_factory=list)
    ipc: list[str] = Field(default_factory=list)
    cpc: list[str] = Field(default_factory=list)


class PatentOriginalFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool = False
    content_type: str = ""
    filename: str = ""
    download_url: str = ""
    storage_path: str = ""


class PatentLookupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: PatentSource
    normalized_number: str
    display_number: str
    basic_info: PatentBasicInfo
    original_file: PatentOriginalFile
    raw_source_refs: dict[str, Any] = Field(default_factory=dict)


class PatentLookupWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    field: str
    message: str
    source: str


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
    title: str = ""
    abstract: str = ""
    ipc: list[str] = Field(default_factory=list)
    cpc: list[str] = Field(default_factory=list)
    applicants: list[str] = Field(default_factory=list)
    inventors: list[str] = Field(default_factory=list)
    application_date: str | None = None
    application_no: str | None = None
    publication_date: str | None = None
    publication_no: str | None = None
    abstract_words: int | None = None
    description_words: int | None = None
    claims_count: int | None = None
    claims_words: int | None = None
    drawings: PatentDrawingsInfo = Field(default_factory=PatentDrawingsInfo)
    original_file_download_url: str | None = None
    warnings: list[PatentLookupWarning] = Field(default_factory=list)
    raw_source_refs: dict[str, Any] = Field(default_factory=dict)


PatentLookupApiResponse = PatentLookupEpResponse | PatentLookupResponse
