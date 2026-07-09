# AGENTS.md

## Purpose

This repository currently contains a frontend application, but a planned Python backend service will be added for patent retrieval and patent file parsing.

Agents working in this repo must treat the backend as a focused service with two core responsibilities only:

1. Retrieve patent metadata and original files by patent number.
2. Parse retrieved patent files for lightweight structural facts such as word counts and drawing-related sections.

Do not expand the scope into general patent analytics, translation, OCR, or document understanding unless the user explicitly asks for it.

## Current Product Scope

### Capability 1: Patent Retrieval

Given a patent number, the service should:

- normalize the number into a canonical publication format;
- detect the source system based on the number prefix and format;
- fetch basic patent metadata;
- fetch or expose the original patent file when available;
- return a stable internal response structure to callers.

Primary external sources:

- `EPO` for `EP...` publications;
- `WIPO PATENTSCOPE` for `WO...` publications.

Agents must not assume that non-`EP` and non-`WO` numbers are in scope unless the user adds them later.

### Capability 2: File Parsing

Given a patent file already retrieved by the service, the parser should initially support:

- total word count;
- section-level word counts when practical;
- whether drawings exist;
- a list of drawing page labels or drawing-related section titles when extractable;
- basic file metadata such as file type, page count, and byte size.

Parsing is intentionally shallow for now. Do not build a full semantic patent parser unless requested.

## Source Rules

### EPO

Use official EPO services as the system of record for `EP` publications.

Preferred split:

- metadata: `OPS`;
- original publication files: `European Publication Server`.

Agents should preserve raw source identifiers from EPO responses whenever possible.

### WIPO

Use official WIPO services as the system of record for `WO` publications.

Important constraints:

- PATENTSCOPE public pages are suitable for manual lookup and light verification;
- automated scraping of public PATENTSCOPE pages should not be the default implementation path;
- if programmatic retrieval for `WO` is implemented, prefer official service endpoints or a design that clearly isolates WIPO-specific retrieval logic for later replacement.

If a WIPO document cannot be fetched programmatically under the current access model, return a clear source-specific error instead of inventing fallback data.

## Repository and Directory Rules

Because this repo is currently frontend-first, agents must keep backend work isolated.

Unless the user specifies another path, place the planned Python backend under:

- `backend/patent_service/`

Recommended structure:

```text
backend/patent_service/
  app/
    api/
    clients/
    models/
    services/
    parsers/
    utils/
    main.py
  tests/
  pyproject.toml
  README.md
```

Do not scatter Python service files across the existing frontend `src/` tree.

## Architecture Expectations

Build the backend with clean boundaries.

Minimum layers:

- `api`: HTTP routes and request/response models only;
- `clients`: low-level EPO/WIPO HTTP clients;
- `services`: orchestration for patent lookup and file retrieval;
- `parsers`: file parsing logic for PDF/XML/HTML-like patent files;
- `models`: internal typed contracts;
- `utils`: normalization, MIME handling, retries, and shared helpers.

Keep source-specific logic out of route handlers.

## Patent Number Handling

Agents must normalize input before retrieval.

Examples:

- `EP1234567A1`
- `WO2026137030A1`
- `WO/2026/137030`

Normalization rules should:

- remove spaces and harmless separators;
- preserve kind codes when present;
- distinguish display format from canonical lookup format;
- reject obviously invalid formats with explicit validation errors.

Do not silently coerce ambiguous numbers into another jurisdiction.

## API Design Guidance

The first useful API surface should stay small.

Suggested endpoints:

- `POST /api/patents/lookup`
- `POST /api/patents/parse`
- `GET /api/health`

Suggested `lookup` request:

```json
{
  "patent_number": "WO2026137030A1",
  "include_original_file": true
}
```

Suggested `lookup` response shape:

```json
{
  "source": "wipo",
  "normalized_number": "WO2026137030A1",
  "display_number": "WO/2026/137030",
  "basic_info": {
    "title": "",
    "abstract": "",
    "publication_date": "",
    "application_number": "",
    "applicants": [],
    "inventors": [],
    "ipc": [],
    "cpc": []
  },
  "original_file": {
    "available": true,
    "content_type": "",
    "filename": "",
    "download_url": "",
    "storage_path": ""
  },
  "raw_source_refs": {}
}
```

Suggested `parse` request:

```json
{
  "file_path": "",
  "file_type": "pdf"
}
```

Suggested `parse` response shape:

```json
{
  "file_type": "pdf",
  "page_count": 0,
  "byte_size": 0,
  "word_count": 0,
  "sections": [],
  "drawings": {
    "has_drawings": false,
    "drawing_labels": [],
    "drawing_page_count": 0
  }
}
```

Agents may refine field names later, but should avoid breaking the top-level shape without user approval.

## Error Handling

All source failures must be explicit and source-aware.

Use structured errors for cases such as:

- invalid patent number format;
- unsupported jurisdiction;
- source returned no result;
- source requires authentication or unavailable permissions;
- source rate limit;
- original file not available;
- parse failure due to unsupported file type.

Do not return partial success as full success.

## Parsing Guidance

Initial parser goals are operational, not academic.

Preferred priorities:

1. reliable file type detection;
2. text extraction with conservative fallbacks;
3. word counting that is stable and testable;
4. drawing detection from section names, page labels, bookmarks, or extracted headings.

Agents should record parser limitations clearly. For example:

- scanned PDF without embedded text;
- malformed XML;
- drawing pages detected only heuristically.

Do not over-claim parsing accuracy.

## Testing Expectations

Every meaningful backend change should add or update tests.

Minimum test coverage should include:

- patent number normalization;
- source routing between `EP` and `WO`;
- lookup error handling;
- response contract stability;
- parser behavior on at least one representative sample per supported file type.

Prefer fixture-driven tests over live network tests.

If live integration tests are added, they must be isolated and skippable.

## Implementation Priorities

When building the service from scratch, follow this order:

1. create backend skeleton and typed contracts;
2. implement patent number normalization and source routing;
3. implement `EP` metadata lookup;
4. implement `EP` original file retrieval;
5. implement `WO` metadata lookup;
6. implement `WO` original file retrieval with explicit access constraints;
7. implement parsing for the first supported file format;
8. add tests and usage documentation.

Do not start with parser complexity before lookup contracts are stable.

## Non-Goals

The following are out of scope unless explicitly requested:

- machine translation of patent content;
- OCR-heavy reconstruction pipelines;
- patent family graph analysis;
- legal status interpretation;
- claim chart generation;
- vector database ingestion;
- bulk crawling across large patent sets.

## Agent Behavior Rules

Agents working on this backend must:

- inspect official source behavior before hardcoding assumptions;
- preserve raw upstream fields when useful for debugging;
- keep retrieval logic deterministic and testable;
- keep parser claims conservative;
- avoid coupling the planned backend to frontend-only concerns.

Agents must not:

- scrape random third-party patent sites as the primary data source;
- silently change response contracts;
- treat WIPO manual UI behavior as proof of stable programmatic API support;
- add heavyweight infrastructure before the core retrieval flow works.

## Documentation Rule

When backend implementation begins, update the backend `README.md` with:

- setup steps;
- required environment variables;
- supported patent number formats;
- supported file types;
- known limitations for EPO and WIPO retrieval.
