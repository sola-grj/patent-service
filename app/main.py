import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import get_settings
from app.errors import PatentServiceError

settings = get_settings()
logger = logging.getLogger("patent_service")

app = FastAPI(title=settings.app_name)
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
