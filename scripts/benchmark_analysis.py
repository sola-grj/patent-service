import argparse
import asyncio
import json
import logging
import time

from app.analysis.ocr import AutoOcrEngine
from app.analysis.service import PatentAnalysisService
from app.clients.epo_ops import EpoOpsClient
from app.clients.epo_publication_server import EpoPublicationServerClient
from app.clients.wipo_patentscope import WipoPatentScopeClient
from app.clients.wipo_patentscope_rest import WipoPatentScopeRestClient
from app.config import Settings
from app.services.patent_lookup import PatentLookupService


def build_service(settings: Settings, ocr: AutoOcrEngine) -> PatentAnalysisService:
    epo_ops = EpoOpsClient(settings)
    publication_server = EpoPublicationServerClient(
        settings.epo_publication_server_url,
        timeout_seconds=settings.request_timeout_seconds,
    )
    lookup = PatentLookupService(
        settings=settings,
        epo_ops_client=epo_ops,
        epo_publication_server_client=publication_server,
        wipo_rest_client=WipoPatentScopeRestClient(settings),
        wipo_soap_client=WipoPatentScopeClient(settings),
    )
    return PatentAnalysisService(
        settings=settings,
        lookup_service=lookup,
        epo_publication_server_client=publication_server,
        epo_ops_client=epo_ops,
        ocr=ocr,
    )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("patent_numbers", nargs="+")
    args = parser.parse_args()
    settings = Settings()
    ocr = AutoOcrEngine(settings)
    service = build_service(settings, ocr)
    print(json.dumps({"ocr": ocr.diagnostics()}, ensure_ascii=False))
    for patent_number in args.patent_numbers:
        started = time.perf_counter()
        response = await service.analyze_patent(patent_number)
        elapsed = time.perf_counter() - started
        result = response.files[0]
        print(
            json.dumps(
                {
                    "patent_number": patent_number,
                    "elapsed_seconds": round(elapsed, 3),
                    "status": response.status,
                    "file_type": result.file_type,
                    "parts": {
                        name: {
                            "word_count": getattr(result.parts, name).word_count,
                            "method": getattr(result.parts, name).method,
                            "status": getattr(result.parts, name).status,
                        }
                        for name in (
                            "abstract",
                            "abstract_drawing",
                            "description",
                            "description_drawings",
                            "claims",
                            "unclassified",
                        )
                    },
                    "total_words": result.total_words,
                    "warnings": [warning.code for warning in result.warnings],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
