from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed, env-driven settings.

    The base uses Postgres only. Scraping settings are inert unless the
    discovery module's routes and handler are registered; the Playwright
    package itself is optional and imported lazily.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    app_name: str = "BidKaro API"
    api_prefix: str = "/api"

    database_url: str = "postgresql+psycopg://postgres@127.0.0.1:55432/bidkaro_v2"

    jwt_secret: str = "replace-with-a-long-random-string-at-least-32-characters"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 10080

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_app_password: str = ""
    smtp_from_name: str = "BidKaro"
    smtp_timeout_seconds: int = 20
    otp_expire_minutes: int = 10
    otp_resend_cooldown_seconds: int = 60
    otp_max_sends_per_hour: int = 5
    otp_max_attempts: int = 5

    cors_origins: str = "http://localhost:5173"

    storage_dir: Path = Path("./storage")
    max_upload_mb: int = 25

    # The scraping module is included in this tree. A deployment that removes
    # it sets FEATURES_DISCOVERY=false so the frontend hides its affordances.
    features_discovery: bool = True

    ai_provider: str = "groq"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    ai_request_timeout_seconds: int = 60
    ai_max_output_tokens: int = 2000

    pdf_max_pages: int = 200
    ocr_enabled: bool = True
    ocr_language: str = "eng"
    ocr_dpi: int = 300
    ocr_max_pages: int = 40
    ocr_timeout_seconds: int = 45
    tesseract_cmd: str = ""

    ai_extraction_max_chunk_chars: int = 4000
    ai_extraction_max_pages_per_chunk: int = 3
    ai_extraction_max_chunks: int = 12
    ai_extraction_max_pages: int = 80

    # Similarity judging is bounded per call and per batch action. The model is
    # asked in batches of ids so a bad answer can only affect the ids it named.
    similarity_batch_size: int = Field(default=8, ge=1, le=25)
    similarity_max_tenders: int = Field(default=40, ge=1, le=200)
    # How many requirements one experience comparison may carry. The model is
    # asked once per run, id in and id out; a skipped id stays "unknown".
    requirement_match_max_requirements: int = Field(default=40, ge=1, le=200)

    # Durable jobs and the single browser lane.
    jobs_enabled: bool = True
    job_workers: int = Field(default=2, ge=1, le=8)
    job_poll_seconds: float = Field(default=0.5, gt=0)
    job_lease_seconds: int = Field(default=120, ge=10)
    job_max_attempts: int = Field(default=3, ge=1)
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = Field(default=60, ge=1)

    # Refuse to start against a database stamped behind the migration head.
    # Deliberately booting a stale database is a bug, not a deployment mode.
    schema_check: bool = True

    # Scraping-module knobs. They are optional in the base and are only used
    # once the module is installed. The base never imports a browser.
    scrape_interval_hours: int = 24
    agent_challenge_wait_seconds: int = 600
    doc_fetch_auto_on: bool = False
    doc_fetch_max_parallel: int = Field(default=1, ge=1, le=1)

    # Portal walk. These are the knobs that explain a 4-8 tender run: they are
    # caps, not bugs, and the status endpoint reports them.
    discovery_directory_url: str = (
        "https://etenders.gov.in/eprocure/app"
        "?page=FrontEndTendersByOrganisation&service=page"
    )
    agent_walk_max_pages: int = Field(default=5, ge=1)
    agent_walk_max_notices: int = Field(default=10, ge=1)
    agent_walk_max_notices_per_org: int = Field(default=4, ge=0)
    agent_walk_max_org_pages: int = Field(default=4, ge=1)
    discovery_max_tenders: int = Field(default=25, ge=1)
    # The nightly sweep reads the whole directory and every list page. Zero
    # means "no cap", which is the point of a sweep: the corpus is built from
    # the portal, not from the profile. The caps below exist only to stop a
    # runaway portal from looping forever, and to keep a manual run bounded.
    sweep_max_organisations: int = Field(default=0, ge=0)
    sweep_max_pages_per_org: int = Field(default=0, ge=0)
    # How many ambiguous titles one adjudication call may carry. The agent is
    # asked in batches, id in and id out, so a bad answer sinks one batch, not
    # the run.
    classification_batch_size: int = Field(default=50, ge=1, le=200)
    classification_max_batches: int = Field(default=20, ge=0)
    browser_headless: bool = True
    browser_timeout_ms: int = Field(default=60_000, ge=1_000)
    browser_proxy_server: str = ""

    @field_validator("cors_origins")
    @classmethod
    def strip_origins(cls, value: str) -> str:
        return value.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def discovery_enabled(self) -> bool:
        return self.features_discovery


@lru_cache
def get_settings() -> Settings:
    return Settings()
