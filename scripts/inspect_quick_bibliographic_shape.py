"""Print only key paths for agent/classification fields in a real WIPO IASR."""

from __future__ import annotations

import argparse
import asyncio

from app.clients.wipo_patentscope_rest import (
    WipoPatentScopeRestClient,
    to_wipo_rest_number,
)
from app.config import Settings
from app.utils.patent_numbers import normalize_patent_number


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wo", default="WO2025078629A1")
    args = parser.parse_args()
    settings = Settings()
    client = WipoPatentScopeRestClient(settings)
    reference = normalize_patent_number(args.wo)
    number = to_wipo_rest_number(reference)
    response = await client._request(  # diagnostic helper
        f"/pct-publications/{number}/ia-status-report",
        accept="application/json",
    )
    payload = response.json()
    matches: list[str] = []
    visit(payload, "$", matches)
    print("\n".join(matches) if matches else "no matching key paths")


def visit(value, path: str, matches: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            next_path = f"{path}.{key}"
            normalized = key.lower().replace("_", "-")
            if (
                "agents" in path.lower()
                or "classif" in path.lower()
                or any(
                token in normalized
                for token in ("agent", "represent", "classif", "ipc", "cpc")
                )
            ):
                matches.append(f"{next_path} ({type(child).__name__})")
            visit(child, next_path, matches)
    elif isinstance(value, list):
        for index, child in enumerate(value[:2]):
            visit(child, f"{path}[{index}]", matches)


if __name__ == "__main__":
    asyncio.run(main())
