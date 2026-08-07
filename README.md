# patent-service backend

Focused Python backend for retrieving and shallowly parsing `EP` and `WO` patent publications.

## Setup

```bash
python -m venv .venv
.venv/Scripts/pip install -e .[dev]
uvicorn app.main:app --reload
```

## Configuration

EPO OPS:

```text
PATENT_SERVICE_EPO_OPS_CONSUMER_KEY
PATENT_SERVICE_EPO_OPS_CONSUMER_SECRET
```

WIPO PATENTSCOPE Web Service 3.0 REST:

```text
PATENT_SERVICE_WIPO_PATENTSCOPE_REST_BASE_URL=https://patentscopews.wipo.int/patentscope-api/v1
PATENT_SERVICE_WIPO_PATENTSCOPE_USERNAME
PATENT_SERVICE_WIPO_PATENTSCOPE_PASSWORD
PATENT_SERVICE_WIPO_LOOKUP_MODE=auto
```

Optional SOAP fallback:

```text
PATENT_SERVICE_WIPO_PATENTSCOPE_SERVICE_URL=<SOAP service or WSDL URL>
```

`PATENT_SERVICE_WIPO_LOOKUP_MODE` accepts `auto`, `rest`, or `soap`:

- `auto`: use REST first and fall back to SOAP only for rate limits, temporary source failures, or invalid upstream payloads.
- `rest`: use REST only.
- `soap`: use the legacy authenticated SOAP webservice only.

Credentials must be injected through the runtime environment and must not be committed. `GET /api/health` reports separate `wipo_rest_configured` and `wipo_soap_configured` flags without exposing credentials.

Submitted-Request cache and service authentication:

```text
PATENT_SERVICE_SUPABASE_URL
PATENT_SERVICE_SUPABASE_SECRET_KEY
PATENT_SERVICE_API_KEY
PATENT_SERVICE_RECEIPT_TTL_SECONDS=86400
PATENT_SERVICE_ANALYSIS_ARTIFACT_TTL_SECONDS=86400
PATENT_SERVICE_ANALYSIS_ARTIFACT_CLEANUP_INTERVAL_SECONDS=300
PATENT_SERVICE_ORIGINAL_FILE_MAX_BYTES=104857600
# Optional; defaults to the OS temporary directory:
PATENT_SERVICE_ANALYSIS_ARTIFACT_DIR
```

`PATENT_SERVICE_API_KEY` must equal the filing application's key of the same
name. The Supabase key must be a server-side secret key.

## API

- `GET /api/health`
- `POST /api/patents/lookup`
- `POST /api/patents/analyze`
- `POST /api/patents/receipts/verify` (service authentication)
- `POST /api/patents/cache` (service authentication)
- `GET /api/patents/cache/requests/{request_id}/file` (service authentication)

```json
{
  "patent_number": "WO2025078629A1",
  "include_original_file": true
}
```

Accepted formats include:

- `EP1234567A1`
- `EP 1234567 A1`
- `EP25188322.9` (European application number)
- `EP 25 188 322.9`
- `EP25188322` (EPODOC application-number form)
- `WO2025078629A1`
- `WO/2025/078629`
- `WO 2025 078629 A1`
- `PCT/AT2025/060357`
- `PCTAT2025060357`
- `CN114302447A` with `source=epo` (national publication via EPO OPS)

National publication numbers are accepted only with an explicit EPO source
override. For multipart analysis requests, send both fields:

```text
patent_number=CN114302447A
source=epo
```

EP publication, EP application, WO publication, and PCT international-application lookup
responses expose the following bibliographic fields at the top level with the
same shape:

- `agents`: structured name, organization, address, and country values;
- `priority_data`: priority number, date, country, and kind;
- `publication_language` and `filing_language`: uppercase language codes;
- `designated_states`: regional systems, country codes, and protection types.
- `related_patent_documents`: other publication numbers in the EPO OPS simple
  patent family, excluding the requested publication itself.

Unavailable upstream values are represented by empty lists or `null`; they are
not inferred.

Interactive lookup is deliberately small: EP calls only OPS `biblio`, and
WO/PCT calls only PATENTSCOPE `ia-status-report`. An official result is returned with
`data_origin: "official"` immediately. Only a definitive official
`source_no_result` may read a submitted-Request cache snapshot; that response
uses `data_origin: "cache_fallback"` and carries cache age metadata. Source
timeouts, authentication failures, rate limits and 5xx responses never use the
cache fallback.

## Five-part word analysis

`POST /api/patents/analyze` accepts `multipart/form-data` and requires exactly
one input mode:

```text
patent_number=WO2026044310A1
```

or one or more `files` fields. Uploads are limited to PDF, DOC and DOCX. The
extension, MIME type and file signature/Office ZIP structure are all checked.
The default limits are 5 files, 50 MiB per file, 100 MiB per request and 300
PDF pages. Configure them with:

```text
PATENT_SERVICE_ANALYSIS_MAX_FILES
PATENT_SERVICE_ANALYSIS_MAX_FILE_BYTES
PATENT_SERVICE_ANALYSIS_MAX_TOTAL_BYTES
PATENT_SERVICE_ANALYSIS_MAX_PDF_PAGES
PATENT_SERVICE_ANALYSIS_TIMEOUT_SECONDS
PATENT_SERVICE_ANALYSIS_MAX_DOCX_ENTRIES
PATENT_SERVICE_ANALYSIS_MAX_DOCX_UNCOMPRESSED_BYTES
PATENT_SERVICE_ANALYSIS_MAX_IMAGE_PIXELS
```

The response contains an independent result for every file plus an additive
`aggregate`. It reports `abstract`, `abstract_drawing`, `description`,
`description_drawings`, `claims` and `unclassified`; each part includes a word
count, status, method and confidence. Similar files are retained and flagged
with `possible_duplicate_content` instead of being silently deducted.

Official-number mode parses WIPO PAMPH ZIP XML/TIFF content. For EPO it first
requests the official OPS bibliographic, description and claims constituents,
then uses Publication Server `document.zip` XML/images to fill missing text and
obtain drawings. EPO grant/search-report kinds such as B1/B2/B3 or A3 are
resolved by trying the corresponding A1 then A2 application publication.
Missing application publications remain explicit errors. The signed analysis
receipt includes a `source_document` descriptor for the publication that was
actually analyzed, so an input grant number cannot later be used to reconstruct
the wrong application-publication URL.

PDF parsing uses a quality-checked text layer first. Empty, garbled or otherwise
unreliable layers fall back to page OCR, while sufficiently large embedded
drawing images on mixed text/image pages are OCRed separately with duplicate
text suppression. DOCX parsing reads the main Word XML and embedded images;
legacy DOC is converted in an isolated LibreOffice profile before entering the
same pipeline. That temporary profile uses the highest macro security level and
is deleted after conversion. Headers, footers, comments, document properties and deleted
revision content are excluded. Text that cannot be classified conservatively
is counted under `unclassified`.

Counting is stable across all inputs: Latin, Cyrillic and Arabic-script tokens
are counted as words; CJK visible characters are counted individually; numeric
and alphanumeric technical tokens are included; punctuation and whitespace are
not. The exact standard is repeated in `counting_standard` on every response.

Local OCR uses an isolated `OcrEngine` interface. `auto` prefers RapidOCR with
ONNX Runtime and PP-OCRv6 small, while Tesseract remains a fault fallback and
explicit backend. PP-OCRv6 handles Chinese, English, Japanese and Latin
languages with one model; Arabic, Cyrillic and Korean inputs use the matching
PP-OCRv5 recognition models. Official publication language is used when
available, while uploaded PDF/DOCX files fall back to script and common-word
language detection.

The OCR engine is process-scoped, so inference sessions remain warm across API
requests. TIFF/PDF scan pages are submitted in bounded batches. Model files use
a persistent user cache by default (`%LOCALAPPDATA%/patent-service/models` on
Windows and `~/.cache/patent-service/models` elsewhere); the Docker image
preloads the common models under `/opt/patent-service/models`. Optional
overrides are:

```text
PATENT_SERVICE_OCR_BACKEND=auto|rapidocr|tesseract
PATENT_SERVICE_OCR_DEFAULT_LANGUAGE=en
PATENT_SERVICE_RAPIDOCR_MODEL_CACHE_DIR
PATENT_SERVICE_RAPIDOCR_ENGINE=onnxruntime|openvino
PATENT_SERVICE_RAPIDOCR_MODEL_TYPE=tiny|small|medium
PATENT_SERVICE_RAPIDOCR_WORKERS=2
PATENT_SERVICE_RAPIDOCR_INTRA_OP_NUM_THREADS=2
PATENT_SERVICE_RAPIDOCR_INTER_OP_NUM_THREADS=1
PATENT_SERVICE_RAPIDOCR_MAX_SIDE=2000
PATENT_SERVICE_OCR_BATCH_SIZE=4
PATENT_SERVICE_TESSERACT_COMMAND
PATENT_SERVICE_OCR_LANGUAGES=eng+deu+fra+spa+por+rus+chi_sim+jpn+kor+ara
PATENT_SERVICE_OCR_TIMEOUT_SECONDS
PATENT_SERVICE_LIBREOFFICE_COMMAND
```

`GET /api/health` reports the selected OCR backend and availability. For
official-number analysis, failure to extract either the description or claims
returns `ocr_failed`/`section_detection_incomplete` instead of publishing a
partial number as the patent total.

For EP publications, the service supplements OPS Published-data with the
official Register bibliographic endpoint:

```text
GET /register/publication/epodoc/{number}/biblio
```

Register retrieval supplies the current agents, complete priority claims,
language of filing, and designated states when those values are available.

## WIPO REST flow

The interactive lookup converts the supplied number to the PATENTSCOPE REST
path form:

- WO publication: `WO2025078629A1` to `WO25078629`;
- PCT international application: `PCT/AT2025/060357` to `AT2025060357`.

It then calls only:

```text
GET /pct-publications/{number}/ia-status-report
```

Background analysis and submitted-Request original-file preparation use the
expanded flow:

```text
GET /pct-publications/{number}/ia-status-report
GET /pct-publications/{number}
GET /documents/{documentId}/pages
GET /documents/{documentId}/pages/{pageId}
GET /documents/{documentId}                 # only when include_original_file=true
```

The analysis endpoint monitors client disconnects. Re-searching, navigating
away, refreshing, or aborting the browser request propagates cancellation into
the parser and OCR batches. Pending OCR work is cancelled, temporary files are
cleaned up, and no cancelled result is published. An inference already running
inside ONNX/Tesseract is allowed to finish its current page before its worker is
reused; remaining pages are not processed.

WIPO patent-number analysis prepares its TIFF-to-PDF rendition while doing the
five-part count. The PDF is copied to a short-lived server-side artifact
directory and the signed analysis receipt contains only its opaque ID, checksum
and expiry metadata, never a local path. EPO analysis does not create a PDF
artifact: it records the resolved Publication Server PDF as a signed
`external_url` source document. WIPO ZIP/PDF workspaces and EPO publication ZIP
workspaces are per-analysis temporary directories and are removed on success,
failure or cancellation.

After a Request and quote are formally submitted, `POST /api/patents/cache`
immediately links EPO Requests to an `external_url` document without adding an
EPO PDF to Storage. WIPO Requests return pending while the verified analysis
artifact is promoted into the private `patent-originals` bucket and globally
deduplicated by patent/document/SHA-256. Drafts never promote files. Expired or
process-lost WIPO artifacts use one official-source re-download after formal
submission as a recovery fallback. Old receipts without `source_document`
continue through this generated-cache compatibility path. Expired artifacts for
abandoned wizards are removed by the periodic cleanup task.

The authenticated Request download endpoint branches on `delivery_strategy`.
`generated_cache` reads the WIPO PDF from private Supabase Storage;
`external_url` proxies only Publication Server URLs matching the configured EPO
base URL. EPO proxy downloads enforce redirect host/path validation, an upstream
timeout, the configured maximum size, PDF content type and PDF signature.

IASR `parties.agents` and `classifications-ipcr` are mapped directly into
`agents` and `basic_info.ipc`. WIPO IASR does not expose CPC, so interactive WO
lookup leaves CPC empty instead of adding another source call. EPO OPS `biblio`
maps its available agents/representatives, IPC and CPC directly.

Requests use HTTP Basic Authentication and `Cookie: OBBasicAuth=fromDialog`. Metadata responses use JSON; the selected `wo-published-application.xml` and original document use `application/octet-stream`.

The published-application XML is used to populate:

- publication and application numbers/dates;
- title and abstract, preferring English;
- IPC, applicants, inventors and structured representatives;
- agents, priority claims, publication/filing languages and designated states;
- description and claims word metrics;
- claim count and drawing indicators.

PATENTSCOPE REST does not define CPC in the supplied schema. When CPC is empty and EPO OPS is configured, the service queries EPO OPS for the same WO publication and merges only CPC. Failure to enrich CPC leaves `cpc=[]` and adds a `cpc_unavailable` warning without failing the WIPO lookup.

When `include_original_file=false`, the service fetches only the lightweight publication XML. When true, it saves the official WIPO document ZIP, orders its TIFF pages using `Pag.lst`, and creates an image-only PDF. `original_file` points to that generated PDF and exposes a relative download URL such as `/api/patents/files/WO2026044310A1.pdf`. The source ZIP remains available in `raw_source_refs.original_archive`; `raw_source_refs.generated_pdf.official_pdf` is always `false` because this is a service-generated rendition, not an official PDF supplied by WIPO.

Direct full-lookup callers use the system temporary WIPO directory by default
or `PATENT_SERVICE_WIPO_STORAGE_DIR` when configured. The product analysis and
submitted-Request cache paths override this with isolated temporary workspaces,
so abandoned searches never leave files in that shared directory.

## WIPO document access

Available document types depend on the account role and the individual application. Selection priority is:

```text
PAMPH -> APBDY -> PUB/A1/A2 -> first available document
```

Common types include `PAMPH`, `APBDY`, `ABSTR`, `DESCR`, `CLAIM`, `DRAWI`, `ISR`, `WOSA`, `IPRP1`, `RO101`, `PDOC`, and `TAB`. Missing documents are reported explicitly; the service does not invent fallback data.

Rate-limit and error-limit response headers are preserved in structured error details:

```text
X-RateLimit-Remaining
X-RateLimit-Reset
X-ErrorLimit-Remaining
X-ErrorLimit-Reset
```

## Tests

```bash
pytest
```

Live WIPO tests are opt-in because they consume the authenticated account quota:

```text
PATENT_SERVICE_RUN_WIPO_LIVE_TESTS=1
PATENT_SERVICE_WIPO_LIVE_PATENT_NUMBER=WO2025078629A1
```

All saved live fixtures must be reviewed and stripped of credentials or private application content before being committed.

## Docker

```bash
docker build -t patent-service:latest .
docker run --rm -p 9098:9098 --env-file .env -v patent-service-tmp:/tmp/patent-service patent-service:latest
```

The image runs the API directly and includes RapidOCR/ONNX Runtime, LibreOffice Writer,
Tesseract and the configured OCR language packs. It does not contain a browser
runtime.

## Known limitations

- EPO and WIPO live lookups require their respective official-service credentials.
- WIPO original publications arrive as official ZIP packages. The service can combine TIFF publication pages into a downloadable image-only PDF, while preserving the ZIP path in `raw_source_refs`; package contents still vary by document and account role.
- Drawing-page counts remain `null` when the XML does not reliably distinguish drawing pages from other page images.
- CPC enrichment is best effort and requires EPO OPS credentials.
- Five-part detection for uploaded PDFs and Word files is conservative and
  heuristic. Uncertain text is returned under `unclassified` with warnings;
  confidence values are not legal or semantic guarantees.
- OCR quality depends on scan resolution, line-art density and the selected
  backend/model. An upload image failure affects only the relevant part where
  possible; an official publication whose description or claims cannot be read
  is rejected so its partial count cannot be mistaken for a complete total.
- Machine translation and deeper semantic patent analysis remain out of scope.
