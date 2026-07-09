# patent-service backend

Focused Python backend for patent retrieval by publication number.

Current scope:

- `EP...` lookup via official EPO services
- `WO...` lookup via official WIPO PATENTSCOPE SOAP webservice
- no file parsing yet

## Setup

1. Create a Python 3.11+ virtual environment.
2. Install dependencies:

```bash
pip install -e .[dev]
```

3. Configure environment variables as needed.

## Environment Variables

Required for live `EP` metadata lookup through EPO OPS:

- `PATENT_SERVICE_EPO_OPS_CONSUMER_KEY`
- `PATENT_SERVICE_EPO_OPS_CONSUMER_SECRET`

Optional overrides:

- `PATENT_SERVICE_EPO_OPS_BASE_URL`
- `PATENT_SERVICE_EPO_OPS_TOKEN_URL`
- `PATENT_SERVICE_EPO_PUBLICATION_SERVER_URL`
- `PATENT_SERVICE_REQUEST_TIMEOUT_SECONDS`
- `PATENT_SERVICE_WIPO_PATENTSCOPE_SERVICE_URL`
- `PATENT_SERVICE_WIPO_PATENTSCOPE_USERNAME`
- `PATENT_SERVICE_WIPO_PATENTSCOPE_PASSWORD`

WIPO requirements:

- `PATENT_SERVICE_WIPO_PATENTSCOPE_SERVICE_URL` should point to the PATENTSCOPE SOAP service or WSDL URL. Example: `https://www.wipo.int/patentscope-webservice/servicesPatentScope?wsdl`
- `PATENT_SERVICE_WIPO_PATENTSCOPE_USERNAME` and `PATENT_SERVICE_WIPO_PATENTSCOPE_PASSWORD` come from a paid PATENTSCOPE webservice subscription, not from the free public search UI alone.

## Run

```bash
uvicorn app.main:app --reload
```

## API

- `GET /api/health`
- `POST /api/patents/lookup`

Example request:

```json
{
  "patent_number": "EP1234567A1",
  "include_original_file": true
}
```

Example `WO` request:

```json
{
  "patent_number": "WO2026137030A1",
  "include_original_file": true
}
```

## Supported Patent Number Formats

Currently accepted input patterns:

- `EP1234567A1`
- `EP 1234567 A1`
- `WO2026137030A1`
- `WO/2026/137030`
- `WO 2026 137030 A1`

Unsupported jurisdictions return explicit structured errors.

## Supported File Types

No parsing support yet. File parsing endpoints are intentionally not implemented in this first cut.

## Known Limitations

- `EP` metadata uses official EPO OPS and therefore requires OPS credentials.
- `EP` original file URLs are exposed via the official European Publication Server once the publication reference is resolved.
- `WO` retrieval uses the official PATENTSCOPE SOAP webservice, which is a paid and authenticated service.
- `WO` original files are materialized into a local temporary file and exposed through `original_file.storage_path`; PATENTSCOPE does not provide the same anonymous public PDF URL pattern as EPO.
- The WIPO SOAP operation signatures are resolved against the authenticated WSDL at runtime. If your subscription exposes a different method contract than expected, the service returns a source-specific upstream error instead of scraping public PATENTSCOPE pages.
- `include_original_file=true` is only supported when the upstream source exposes a publication document payload for that record.
