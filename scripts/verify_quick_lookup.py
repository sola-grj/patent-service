"""Run one real EP and WO interactive lookup and print endpoint/timing evidence."""

from __future__ import annotations

import argparse
import asyncio
import time

from app.clients.epo_ops import EpoOpsClient
from app.clients.epo_publication_server import EpoPublicationServerClient
from app.clients.wipo_patentscope import WipoPatentScopeClient
from app.clients.wipo_patentscope_rest import WipoPatentScopeRestClient
from app.config import Settings
from app.models.patents import PatentLookupRequest
from app.services.patent_lookup import PatentLookupService


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ep", default="EP3987654A1")
    parser.add_argument("--wo", default="WO2025078629A1")
    args = parser.parse_args()
    settings = Settings()
    epo = EpoOpsClient(settings)
    if settings.epo_ops_configured:
        await epo.warmup()
    wipo = WipoPatentScopeRestClient(settings)
    if settings.wipo_rest_configured:
        await wipo.warmup()
    service = PatentLookupService(
        settings=settings,
        epo_ops_client=epo,
        epo_publication_server_client=EpoPublicationServerClient(
            settings.epo_publication_server_url,
            timeout_seconds=settings.request_timeout_seconds,
        ),
        wipo_rest_client=wipo,
        wipo_soap_client=WipoPatentScopeClient(settings),
    )
    for patent_number in (args.ep, args.wo):
        started = time.perf_counter()
        response = await service.lookup_patent(
            PatentLookupRequest(
                patent_number=patent_number,
                include_original_file=False,
            )
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if response.source.value == "epo":
            endpoint = response.raw_source_refs["ops_biblio"]["endpoint"]
            agents = len(response.agents)
            ipc = len(response.ipc)
            cpc = len(response.cpc)
        else:
            endpoint = response.raw_source_refs["iasr_request"]
            agents = len(response.agents)
            ipc = len(response.basic_info.ipc)
            cpc = len(response.basic_info.cpc)
        print(
            f"{response.source.value}: number={response.normalized_number} "
            f"origin={response.data_origin} elapsed_ms={elapsed_ms} "
            f"endpoint={endpoint} agents={agents} ipc={ipc} cpc={cpc}"
        )


if __name__ == "__main__":
    asyncio.run(main())
