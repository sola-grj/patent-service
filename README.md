# patent-service backend

Focused Python backend for patent retrieval by publication number.

Current scope:

- `EP...` lookup via official EPO services
- `WO...` lookup via WIPO PATENTSCOPE public detail pages or official PATENTSCOPE SOAP webservice
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
- `PATENT_SERVICE_WIPO_LOOKUP_MODE`
- `PATENT_SERVICE_WIPO_PUBLIC_BASE_URL`
- `PATENT_SERVICE_WIPO_SELENIUM_CHROME_BINARY`
- `PATENT_SERVICE_WIPO_SELENIUM_HEADLESS`
- `PATENT_SERVICE_WIPO_SELENIUM_TIMEOUT_SECONDS`

WIPO requirements:

- `PATENT_SERVICE_WIPO_PATENTSCOPE_SERVICE_URL` should point to the PATENTSCOPE SOAP service or WSDL URL. Example: `https://www.wipo.int/patentscope-webservice/servicesPatentScope?wsdl`
- `PATENT_SERVICE_WIPO_PATENTSCOPE_USERNAME` and `PATENT_SERVICE_WIPO_PATENTSCOPE_PASSWORD` come from a paid PATENTSCOPE webservice subscription, not from the free public search UI alone.
- `PATENT_SERVICE_WIPO_LOOKUP_MODE` accepts `auto`, `public_page`, or `soap`. Default: `auto`.
- `PATENT_SERVICE_WIPO_PUBLIC_BASE_URL` defaults to `https://patentscope.wipo.int/search/en`.
- `PATENT_SERVICE_WIPO_SELENIUM_CHROME_BINARY` is optional when Selenium cannot auto-detect Chrome.
- `PATENT_SERVICE_WIPO_SELENIUM_HEADLESS` defaults to `false`. The public WIPO page may reject headless Chrome, so the default lookup path uses a real browser window moved to the background.
- `PATENT_SERVICE_WIPO_SELENIUM_TIMEOUT_SECONDS` defaults to `45`.

## Run

```bash
uvicorn app.main:app --reload
```

## Selenium Probe

The standalone Selenium probe uses the same field extraction rules as the WIPO `public_page` lookup path.

Run the probe:

```bash
python scripts/wipo_selenium_probe.py --doc-id WO2026044310 --cid P11-MREAVK-01901-1
```

The probe opens the target detail page in real Chrome via Selenium, classifies the page state, and, when the page is bibliographic, extracts the basic patent fields. It writes:

- a JSON summary to stdout
- `detail.html`
- `detail.png`

Default artifact directory: `artifacts/wipo-selenium-probe/`

If you want to test the logged-in path explicitly:

```bash
python scripts/wipo_selenium_probe.py --login --username YOUR_USER --password YOUR_PASS --doc-id WO2026044310 --cid P11-MREAVK-01901-1
```

The probe now runs with a real Chrome window by default. Use `--headless` only when you explicitly want to test headless behavior.

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
- `WO` lookup in `public_page` mode is a low-frequency, best-effort metadata path built on top of PATENTSCOPE detail pages. It is not a high-stability bulk data channel.
- `WO` lookup in `public_page` mode now drives a real Chrome session through Selenium and parses the rendered PATENTSCOPE bibliographic DOM. This keeps extraction closer to the browser-visible page, but it also means the service needs a usable local Chrome runtime.
- `WO` public-page mode now uses Selenium to switch the visible PATENTSCOPE tabs and extract description text, claims text, drawing-page presence, and published-application document links from the rendered DOM.
- `WO` public-page mode does not materialize the original file into local storage. When `include_original_file=true`, the lookup response can expose the published-application download link found in the `Documents` tab, while `auto` mode with configured SOAP credentials can still fall back to the authenticated SOAP client for a local `storage_path`.
- `WO` original files fetched through SOAP are materialized into a local temporary file and exposed through `original_file.storage_path`; PATENTSCOPE does not provide the same anonymous public PDF URL pattern as EPO.
- The WIPO SOAP operation signatures are resolved against the authenticated WSDL at runtime. If your subscription exposes a different method contract than expected, the service returns a source-specific upstream error instead of scraping public PATENTSCOPE pages.
- WIPO PATENTSCOPE public Terms of Use explicitly prohibit automated queries, bulk downloading, and web scraping as of the page revision labeled `Last updated: October 2025`. Use public-page mode only with that operational and compliance risk understood.
- Headless Chrome is not yet a verified deployment path here. If `public_page` mode is required in production, validate the exact Chrome/driver/runtime combination first.
- `include_original_file=true` is only supported when the upstream source exposes a publication document payload for that record.
