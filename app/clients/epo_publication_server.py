import httpx

from app.errors import ErrorCode, PatentServiceError


class EpoPublicationServerClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def build_pdf_download_url(
        self, *, country_code: str, doc_number: str, kind_code: str
    ) -> str:
        patent_number = f"{country_code}{doc_number}NW{kind_code}"
        return f"{self._base_url.rstrip('/')}/{patent_number}/document.pdf"

    def build_archive_download_url(
        self, *, country_code: str, doc_number: str, kind_code: str
    ) -> str:
        patent_number = f"{country_code}{doc_number}NW{kind_code}"
        return f"{self._base_url.rstrip('/')}/{patent_number}/document.zip"

    async def download_archive(
        self, *, country_code: str, doc_number: str, kind_code: str
    ) -> bytes:
        url = self.build_archive_download_url(
            country_code=country_code,
            doc_number=doc_number,
            kind_code=kind_code,
        )
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(url, headers={"Accept": "application/zip"})
        except httpx.HTTPError as exc:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                status_code=502,
                message="EPO Publication Server could not be reached.",
                source="epo",
                details={"url": url, "error": str(exc)},
            ) from exc
        if response.status_code == 404:
            raise PatentServiceError(
                code=ErrorCode.ORIGINAL_FILE_NOT_AVAILABLE,
                status_code=404,
                message="EPO Publication Server did not expose a structured archive.",
                source="epo",
                details={"url": url, "kind_code": kind_code},
            )
        if response.status_code in {401, 403}:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_ACCESS_DENIED,
                status_code=502,
                message="EPO Publication Server denied archive access.",
                source="epo",
                details={"url": url, "upstream_status": response.status_code},
            )
        if response.status_code == 429:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_RATE_LIMIT,
                status_code=503,
                message="EPO Publication Server rate limit was reached.",
                source="epo",
                details={"url": url},
            )
        if response.status_code >= 400:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                status_code=502,
                message="EPO Publication Server returned an error.",
                source="epo",
                details={"url": url, "upstream_status": response.status_code},
            )
        if not response.content.startswith(b"PK"):
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="EPO Publication Server returned a non-ZIP archive response.",
                source="epo",
                details={"url": url, "content_type": response.headers.get("content-type", "")},
            )
        return response.content

    async def download_pdf(
        self, *, country_code: str, doc_number: str, kind_code: str
    ) -> bytes:
        url = self.build_pdf_download_url(
            country_code=country_code,
            doc_number=doc_number,
            kind_code=kind_code,
        )
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(url, headers={"Accept": "application/pdf"})
        except httpx.HTTPError as exc:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                status_code=502,
                message="EPO Publication Server PDF could not be reached.",
                source="epo",
                details={"url": url, "error": str(exc)},
            ) from exc
        if response.status_code == 404:
            raise PatentServiceError(
                code=ErrorCode.ORIGINAL_FILE_NOT_AVAILABLE,
                status_code=404,
                message="EPO Publication Server did not expose the original PDF.",
                source="epo",
                details={"url": url, "kind_code": kind_code},
            )
        if response.status_code in {401, 403}:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_ACCESS_DENIED,
                status_code=502,
                message="EPO Publication Server denied PDF access.",
                source="epo",
                details={"url": url, "upstream_status": response.status_code},
            )
        if response.status_code == 429:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_RATE_LIMIT,
                status_code=503,
                message="EPO Publication Server rate limit was reached.",
                source="epo",
                details={"url": url},
            )
        if response.status_code >= 400:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                status_code=502,
                message="EPO Publication Server returned an error for the PDF.",
                source="epo",
                details={"url": url, "upstream_status": response.status_code},
            )
        if not response.content.startswith(b"%PDF-"):
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="EPO Publication Server returned a non-PDF response.",
                source="epo",
                details={
                    "url": url,
                    "content_type": response.headers.get("content-type", ""),
                },
            )
        return response.content
