import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import (
    get_artifact_store,
    get_epo_ops_client,
    get_wipo_rest_client,
    router,
)
from app.config import get_settings
from app.errors import PatentServiceError
from app.logging_config import configure_app_logging

settings = get_settings()
logger = configure_app_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = []
    if settings.epo_ops_configured:
        tasks.append(asyncio.create_task(_warm_epo_token()))
    if settings.wipo_rest_configured:
        tasks.append(asyncio.create_task(_warm_wipo_connection()))
    tasks.append(asyncio.create_task(_cleanup_analysis_artifacts()))
    try:
        yield
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


async def _warm_epo_token() -> None:
    try:
        await get_epo_ops_client().warmup()
        logger.info("EPO OPS token warmup completed")
    except Exception:
        logger.exception("EPO OPS token warmup failed")


async def _warm_wipo_connection() -> None:
    try:
        await get_wipo_rest_client().warmup()
        logger.info("WIPO PATENTSCOPE connection warmup completed")
    except Exception:
        logger.exception("WIPO PATENTSCOPE connection warmup failed")


async def _cleanup_analysis_artifacts() -> None:
    interval = max(settings.analysis_artifact_cleanup_interval_seconds, 60)
    store = get_artifact_store()
    while True:
        try:
            removed = await asyncio.to_thread(store.cleanup_expired)
            if removed:
                logger.info("Expired analysis artifacts removed count=%s", removed)
        except Exception:
            logger.exception("Analysis artifact cleanup failed")
        await asyncio.sleep(interval)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(router, prefix=settings.api_prefix)


@app.exception_handler(PatentServiceError)
async def patent_service_error_handler(
    request: Request, exc: PatentServiceError
) -> JSONResponse:
    logger.warning(
        "request failed method=%s path=%s code=%s status=%s source=%s details=%s",
        request.method,
        request.url.path,
        exc.code,
        exc.status_code,
        exc.source,
        exc.details,
    )
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())
