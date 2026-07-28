"""Remove patent cache rows that have no submitted Request relationship.

Dry-run is the default. Pass --execute only after reviewing the exact counts.
The script deliberately aborts when a target document is still referenced by a
request file or when the expected target counts do not match.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import quote

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expect-patents", type=int)
    parser.add_argument("--expect-documents", type=int)
    args = parser.parse_args()
    if args.env_file:
        load_env_file(args.env_file)
    base_url = required_env("NEXT_PUBLIC_SUPABASE_URL", "SUPABASE_URL").rstrip("/")
    key = required_env("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY")
    headers = {"apikey": key}

    with httpx.Client(timeout=60, follow_redirects=True, headers=headers) as client:
        snapshot = inspect(client, base_url)
        print_summary("dry-run" if not args.execute else "pre-delete", snapshot)
        verify_rls(base_url)
        assert_expected(snapshot, args.expect_patents, args.expect_documents)
        if not args.execute:
            verify_remaining_documents(
                client, base_url, snapshot["remaining_documents"]
            )
            return
        if snapshot["referenced_target_document_ids"]:
            raise SystemExit(
                "Abort: at least one target patent document is referenced by request_files."
            )
        delete_storage_objects(client, base_url, snapshot["target_documents"])
        delete_rows(client, base_url, snapshot)
        verified = inspect(client, base_url)
        print_summary("verified", verified)
        if verified["target_patents"] or verified["target_documents"]:
            raise SystemExit("Cleanup verification failed: unlinked cache rows remain.")
        verify_remaining_documents(client, base_url, verified["remaining_documents"])


def inspect(client: httpx.Client, base_url: str) -> dict:
    patents = rest_get(client, base_url, "patents", {"select": "id,normalized_number"})
    relationships = rest_get(
        client, base_url, "request_patents", {"select": "id,request_id,patent_id"}
    )
    documents = rest_get(
        client,
        base_url,
        "patent_documents",
        {
            "select": (
                "id,patent_id,storage_bucket,storage_path,byte_size,"
                "original_filename"
            )
        },
    )
    linked_ids = {row["patent_id"] for row in relationships if row.get("patent_id")}
    target_patents = [row for row in patents if row["id"] not in linked_ids]
    target_ids = {row["id"] for row in target_patents}
    target_documents = [
        row for row in documents if row.get("patent_id") in target_ids
    ]
    target_document_ids = {row["id"] for row in target_documents}
    request_files = rest_get(
        client,
        base_url,
        "request_files",
        {"select": "id,request_id,patent_document_id"},
    )
    referenced_targets = {
        row["patent_document_id"]
        for row in request_files
        if row.get("patent_document_id") in target_document_ids
    }
    return {
        "patents": patents,
        "relationships": relationships,
        "documents": documents,
        "target_patents": target_patents,
        "target_documents": target_documents,
        "remaining_documents": [
            row for row in documents if row.get("patent_id") in linked_ids
        ],
        "referenced_target_document_ids": referenced_targets,
    }


def print_summary(label: str, snapshot: dict) -> None:
    target_bytes = sum(int(row.get("byte_size") or 0) for row in snapshot["target_documents"])
    print(
        f"{label}: patents={len(snapshot['patents'])} "
        f"request_patents={len(snapshot['relationships'])} "
        f"documents={len(snapshot['documents'])} "
        f"unlinked_patents={len(snapshot['target_patents'])} "
        f"target_documents={len(snapshot['target_documents'])} "
        f"target_bytes={target_bytes} "
        f"referenced_target_documents={len(snapshot['referenced_target_document_ids'])}"
    )


def assert_expected(snapshot: dict, patents: int | None, documents: int | None) -> None:
    if patents is not None and len(snapshot["target_patents"]) != patents:
        raise SystemExit("Abort: unlinked patent count differs from --expect-patents.")
    if documents is not None and len(snapshot["target_documents"]) != documents:
        raise SystemExit("Abort: document count differs from --expect-documents.")


def delete_storage_objects(
    client: httpx.Client, base_url: str, documents: list[dict]
) -> None:
    for document in documents:
        bucket = quote(document["storage_bucket"], safe="")
        path = "/".join(
            quote(part, safe="") for part in document["storage_path"].split("/")
        )
        response = client.delete(f"{base_url}/storage/v1/object/{bucket}/{path}")
        if response.status_code not in {200, 204}:
            raise SystemExit(
                "Abort: a target Storage object could not be deleted "
                f"(status={response.status_code})."
            )


def delete_rows(client: httpx.Client, base_url: str, snapshot: dict) -> None:
    patent_ids = [row["id"] for row in snapshot["target_patents"]]
    if not patent_ids:
        return
    filter_value = f"in.({','.join(patent_ids)})"
    for table in ("patent_lookup_events", "patent_lookup_aliases", "patent_documents"):
        rest_delete(client, base_url, table, {"patent_id": filter_value})
    rest_delete(client, base_url, "patents", {"id": filter_value})


def verify_remaining_documents(
    client: httpx.Client, base_url: str, documents: list[dict]
) -> None:
    missing = 0
    mismatched = 0
    public_leaks = 0
    for document in documents:
        bucket = quote(document["storage_bucket"], safe="")
        path = "/".join(
            quote(part, safe="") for part in document["storage_path"].split("/")
        )
        response = client.get(f"{base_url}/storage/v1/object/{bucket}/{path}")
        if response.status_code != 200:
            missing += 1
        elif len(response.content) != int(document.get("byte_size") or 0):
            mismatched += 1
        public_response = httpx.get(
            f"{base_url}/storage/v1/object/public/{bucket}/{path}",
            timeout=30,
            follow_redirects=True,
        )
        if public_response.status_code == 200:
            public_leaks += 1
    print(
        f"storage-verification: remaining_documents={len(documents)} "
        f"missing={missing} size_mismatches={mismatched} "
        f"public_leaks={public_leaks}"
    )
    if missing or mismatched or public_leaks:
        raise SystemExit("Remaining patent document Storage verification failed.")


def verify_rls(base_url: str) -> None:
    anon_key = (
        os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY")
        or os.environ.get("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY")
    )
    if not anon_key:
        print("rls-verification: skipped (anon key unavailable)")
        return
    response = httpx.get(
        f"{base_url}/rest/v1/patents",
        params={"select": "id", "limit": "1"},
        headers={"apikey": anon_key, "Authorization": f"Bearer {anon_key}"},
        timeout=30,
    )
    if response.status_code >= 400:
        print(f"rls-verification: denied status={response.status_code}")
        return
    rows = response.json()
    print(f"rls-verification: anon_rows={len(rows)}")
    if rows:
        raise SystemExit("Patent cache RLS verification failed: anon can read rows.")


def rest_get(
    client: httpx.Client, base_url: str, table: str, params: dict[str, str]
) -> list[dict]:
    response = client.get(f"{base_url}/rest/v1/{table}", params=params)
    response.raise_for_status()
    return response.json()


def rest_delete(
    client: httpx.Client, base_url: str, table: str, params: dict[str, str]
) -> None:
    response = client.delete(f"{base_url}/rest/v1/{table}", params=params)
    response.raise_for_status()


def load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def required_env(*names: str) -> str:
    for name in names:
        if value := os.environ.get(name):
            return value
    raise SystemExit(f"Missing environment variable: {' or '.join(names)}")


if __name__ == "__main__":
    main()
