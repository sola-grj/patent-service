import httpx
from urllib.parse import urljoin, urlsplit

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
        payload, _ = await self.download_pdf_url(url, max_bytes=100 * 1024 * 1024)
        return payload

    async def download_pdf_url(
        self,
        url: str,
        *,
        max_bytes: int,
    ) -> tuple[bytes, str]:
        self._validate_pdf_url(url)
        payload = b""
        content_type = ""
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                target_url = url
                for _ in range(6):
                    async with client.stream(
                        "GET", target_url, headers={"Accept": "application/pdf"}
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise _invalid_pdf_response(
                                    target_url,
                                    "EPO Publication Server returned an invalid redirect.",
                                )
                            target_url = urljoin(target_url, location)
                            self._validate_pdf_url(target_url)
                            continue
                        payload, content_type = await self._read_pdf_response(
                            response,
                            url=target_url,
                            max_bytes=max_bytes,
                        )
                        break
                else:
                    raise _invalid_pdf_response(
                        url,
                        "EPO Publication Server redirected too many times.",
                    )
        except httpx.HTTPError as exc:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                status_code=502,
                message="EPO Publication Server PDF could not be reached.",
                source="epo",
                details={"url": url, "error": str(exc)},
            ) from exc
        if not payload.startswith(b"%PDF-"):
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="EPO Publication Server returned a non-PDF response.",
                source="epo",
                details={
                    "url": url,
                    "content_type": content_type,
                },
            )
        return payload, content_type

    async def _read_pdf_response(
        self,
        response: httpx.Response,
        *,
        url: str,
        max_bytes: int,
    ) -> tuple[bytes, str]:
        if response.status_code == 404:
            raise PatentServiceError(
                code=ErrorCode.ORIGINAL_FILE_NOT_AVAILABLE,
                status_code=404,
                message="EPO Publication Server did not expose the original PDF.",
                source="epo",
                details={"url": url},
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
        content_type = response.headers.get("content-type", "").split(";", 1)[
            0
        ].lower()
        if content_type not in {"application/pdf", "application/octet-stream"}:
            raise _invalid_pdf_response(
                url,
                "EPO Publication Server returned an invalid PDF content type.",
                content_type=content_type,
            )
        declared_header = response.headers.get("content-length")
        try:
            declared_size = int(declared_header or 0)
        except ValueError as exc:
            raise _invalid_pdf_response(
                url,
                "EPO Publication Server returned an invalid content length.",
            ) from exc
        if declared_size < 0:
            raise _invalid_pdf_response(
                url,
                "EPO Publication Server returned an invalid content length.",
            )
        if declared_size > max_bytes:
            raise _pdf_too_large(url, max_bytes)
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise _pdf_too_large(url, max_bytes)
            chunks.append(chunk)
        return b"".join(chunks), content_type

    def _validate_pdf_url(self, url: str) -> None:
        base = urlsplit(self._base_url)
        target = urlsplit(url)
        base_path = base.path.rstrip("/") + "/"
        if (
            target.scheme != base.scheme
            or target.netloc != base.netloc
            or not target.path.startswith(base_path)
            or not target.path.endswith("/document.pdf")
            or target.username
            or target.password
            or target.fragment
        ):
            raise PatentServiceError(
                code=ErrorCode.SOURCE_ACCESS_DENIED,
                status_code=502,
                message="The external EPO document URL is not allowed.",
                source="epo",
                details={"url": url},
            )


def _pdf_too_large(url: str, max_bytes: int) -> PatentServiceError:
    return PatentServiceError(
        code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
        status_code=502,
        message="The EPO PDF exceeds the configured download limit.",
        source="epo",
        details={"url": url, "max_bytes": max_bytes},
    )


def _invalid_pdf_response(
    url: str,
    message: str,
    *,
    content_type: str | None = None,
) -> PatentServiceError:
    details: dict[str, str] = {"url": url}
    if content_type is not None:
        details["content_type"] = content_type
    return PatentServiceError(
        code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
        status_code=502,
        message=message,
        source="epo",
        details=details,
    )
