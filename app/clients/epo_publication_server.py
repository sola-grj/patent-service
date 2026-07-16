class EpoPublicationServerClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    def build_pdf_download_url(
        self, *, country_code: str, doc_number: str, kind_code: str
    ) -> str:
        patent_number = f"{country_code}{doc_number}NW{kind_code}"
        return f"{self._base_url.rstrip('/')}/{patent_number}/document.pdf"
