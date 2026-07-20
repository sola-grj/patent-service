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
    request_timeout_seconds: float = 20.0

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
