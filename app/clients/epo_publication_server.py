from urllib.parse import urlencode


class EpoPublicationServerClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    def build_pdf_download_url(
        self, *, country_code: str, doc_number: str, kind_code: str
    ) -> str:
        query = urlencode({"cc": country_code, "pn": doc_number, "ki": kind_code})
        return f"{self._base_url}?{query}"
