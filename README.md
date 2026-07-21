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

The image runs the API directly and does not contain a browser runtime.

## Known limitations

- EPO and WIPO live lookups require their respective official-service credentials.
- WIPO original publications arrive as official ZIP packages. The service can combine TIFF publication pages into a downloadable image-only PDF, while preserving the ZIP path in `raw_source_refs`; package contents still vary by document and account role.
- Drawing-page counts remain `null` when the XML does not reliably distinguish drawing pages from other page images.
- CPC enrichment is best effort and requires EPO OPS credentials.
- The service performs shallow structural parsing only; OCR, translation and semantic patent analysis are out of scope.
