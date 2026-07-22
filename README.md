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

## API

- `GET /api/health`
- `POST /api/patents/lookup`
- `POST /api/patents/analyze`

```json
{
  "patent_number": "WO2025078629A1",
  "include_original_file": true
}
```

Accepted formats include:

- `EP1234567A1`
- `EP 1234567 A1`
- `WO2025078629A1`
- `WO/2025/078629`
- `WO 2025 078629 A1`

Both EP and WO lookup responses expose the following bibliographic fields at the
top level with the same shape:

- `agents`: structured name, organization, address, and country values;
- `priority_data`: priority number, date, country, and kind;
- `publication_language` and `filing_language`: uppercase language codes;
- `designated_states`: regional systems, country codes, and protection types.
- `related_patent_documents`: other publication numbers in the EPO OPS simple
  patent family, excluding the requested publication itself.

Unavailable upstream values are represented by empty lists or `null`; they are
not inferred.

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
Missing application publications remain explicit errors.

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

The service converts a normalized four-digit-year WO number to the REST form, for example `WO2025078629A1` to `WO25078629`, then calls:

```text
GET /pct-publications/{number}/ia-status-report
GET /pct-publications/{number}
GET /documents/{documentId}/pages
GET /documents/{documentId}/pages/{pageId}
GET /documents/{documentId}                 # only when include_original_file=true
```

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

Generated files are stored under the system temporary directory by default. Set `PATENT_SERVICE_WIPO_STORAGE_DIR` to use a persistent directory.

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
