from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "patent-service"
    api_prefix: str = "/api"
    epo_ops_base_url: str = "https://ops.epo.org/3.2/rest-services"
    epo_ops_token_url: str = "https://ops.epo.org/3.2/auth/accesstoken"
    epo_ops_consumer_key: str | None = None
    epo_ops_consumer_secret: str | None = None
    epo_publication_server_url: str = (
        "https://data.epo.org/publication-server/rest/v1.2/patents"
    )
    wipo_patentscope_rest_base_url: str = (
        "https://patentscopews.wipo.int/patentscope-api/v1"
    )
    wipo_patentscope_service_url: str | None = None
    wipo_patentscope_username: str | None = None
    wipo_patentscope_password: str | None = None
    wipo_lookup_mode: Literal["auto", "rest", "soap"] = "auto"
    wipo_storage_dir: str | None = None
    request_timeout_seconds: float = 20.0
    analysis_max_file_bytes: int = 50 * 1024 * 1024
    analysis_max_total_bytes: int = 100 * 1024 * 1024
    analysis_max_files: int = 5
    analysis_max_pdf_pages: int = 300
    analysis_timeout_seconds: float = 600.0
    analysis_artifact_dir: str | None = None
    analysis_artifact_ttl_seconds: int = 24 * 60 * 60
    analysis_artifact_cleanup_interval_seconds: int = 5 * 60
    analysis_max_docx_entries: int = 5000
    analysis_max_docx_uncompressed_bytes: int = 200 * 1024 * 1024
    analysis_max_image_pixels: int = 50_000_000
    ocr_backend: Literal["auto", "rapidocr", "tesseract"] = "auto"
    ocr_default_language: str = "en"
    rapidocr_model_cache_dir: str | None = None
    rapidocr_engine: Literal["onnxruntime", "openvino"] = "onnxruntime"
    rapidocr_model_type: Literal["tiny", "small", "medium"] = "small"
    rapidocr_workers: int = 2
    rapidocr_intra_op_num_threads: int = 2
    rapidocr_inter_op_num_threads: int = 1
    rapidocr_max_side: int = 2000
    ocr_batch_size: int = 4
    ocr_languages: str = "eng+deu+fra+spa+por+rus+chi_sim+jpn+kor+ara"
    ocr_timeout_seconds: float = 60.0
    tesseract_command: str | None = None
    libreoffice_command: str | None = None
    supabase_url: str | None = None
    supabase_secret_key: str | None = None
    api_key: str | None = None
    receipt_ttl_seconds: int = 24 * 60 * 60
    cache_fresh_days: int = 7
    cache_force_refresh_days: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PATENT_SERVICE_",
        extra="ignore",
    )

    @property
    def epo_ops_configured(self) -> bool:
        return bool(self.epo_ops_consumer_key and self.epo_ops_consumer_secret)

    @property
    def wipo_rest_configured(self) -> bool:
        return bool(
            self.wipo_patentscope_username and self.wipo_patentscope_password
        )

    @property
    def wipo_soap_configured(self) -> bool:
        return bool(
            self.wipo_patentscope_service_url
            and self.wipo_patentscope_username
            and self.wipo_patentscope_password
        )

    @property
    def wipo_patentscope_configured(self) -> bool:
        """Backward-compatible alias for the SOAP client."""
        return self.wipo_soap_configured

    @property
    def patent_cache_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_secret_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
