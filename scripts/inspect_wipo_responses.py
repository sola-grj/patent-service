import argparse
import asyncio
import json

import httpx

from app.clients.wipo_patentscope_rest import (
    parse_available_documents,
    select_publication_document,
    select_publication_xml,
    to_wipo_rest_number,
)
from app.config import Settings
from app.utils.patent_numbers import normalize_patent_number


def print_response(path: str, response: httpx.Response) -> None:
    print(f"\n=== GET {path}")
    print(f"status: {response.status_code}")
    print(f"content-type: {response.headers.get('content-type', '')}")
    print(f"x-ratelimit-remaining: {response.headers.get('x-ratelimit-remaining', '')}")
    print(f"x-ratelimit-reset: {response.headers.get('x-ratelimit-reset', '')}")
    if "json" in response.headers.get("content-type", "").lower():
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    else:
        print(response.text[:12_000])


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("patent_number", nargs="?", default="WO2025078629A1")
    args = parser.parse_args()
    settings = Settings()
    if not settings.wipo_rest_configured:
        raise SystemExit("WIPO REST credentials are not configured")

    rest_number = to_wipo_rest_number(normalize_patent_number(args.patent_number))
    auth = httpx.BasicAuth(
        settings.wipo_patentscope_username or "",
        settings.wipo_patentscope_password or "",
    )
    headers = {"Cookie": "OBBasicAuth=fromDialog"}
    async with httpx.AsyncClient(
        base_url=settings.wipo_patentscope_rest_base_url.rstrip("/"),
        auth=auth,
        headers=headers,
        timeout=30,
        follow_redirects=True,
    ) as client:
        iasr_path = f"/pct-publications/{rest_number}/ia-status-report"
        response = await client.get(iasr_path, headers={"Accept": "application/json"})
        print_response(iasr_path, response)
        response.raise_for_status()

        documents_path = f"/pct-publications/{rest_number}"
        response = await client.get(
            documents_path, headers={"Accept": "application/json"}
        )
        print_response(documents_path, response)
        response.raise_for_status()
        selected = select_publication_document(parse_available_documents(response.json()))
        if selected is None:
            return

        pages_path = f"/documents/{selected.document_id}/pages"
        response = await client.get(pages_path, headers={"Accept": "application/json"})
        print_response(pages_path, response)
        response.raise_for_status()
        page_names = response.json().get("content", [])
        xml_page = select_publication_xml(page_names)
        if not xml_page:
            return

        xml_path = f"/documents/{selected.document_id}/pages/{xml_page}"
        response = await client.get(
            xml_path, headers={"Accept": "application/octet-stream"}
        )
        print_response(xml_path, response)
        response.raise_for_status()


if __name__ == "__main__":
    asyncio.run(main())
