"""Validate every Request-linked patent snapshot against the current API model."""

from __future__ import annotations

import asyncio

import httpx

from app.cache.supabase import SupabasePatentCache
from app.config import Settings
from app.utils.patent_numbers import normalize_patent_number


async def main() -> None:
    settings = Settings()
    headers = {"apikey": settings.supabase_secret_key or ""}
    async with httpx.AsyncClient(
        base_url=(settings.supabase_url or "").rstrip("/"),
        headers=headers,
        timeout=30,
    ) as client:
        relationships = (
            await client.get(
                "/rest/v1/request_patents",
                params={"select": "patent_id,patent_number"},
            )
        ).raise_for_status().json()
        patents = (
            await client.get(
                "/rest/v1/patents",
                params={"select": "id,normalized_number"},
            )
        ).raise_for_status().json()
    linked_ids = {row["patent_id"] for row in relationships if row.get("patent_id")}
    cache = SupabasePatentCache(settings)
    valid = 0
    invalid: list[str] = []
    for patent in patents:
        if patent["id"] not in linked_ids:
            continue
        reference = normalize_patent_number(patent["normalized_number"])
        fallback = await cache.find_lookup_fallback(reference)
        if fallback:
            valid += 1
        else:
            invalid.append(patent["normalized_number"])
    print(
        f"linked_patents={len(linked_ids)} valid_fallback_snapshots={valid} "
        f"invalid_fallback_snapshots={len(invalid)}"
    )
    if invalid:
        print("invalid_numbers=" + ",".join(invalid))
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
