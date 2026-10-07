"""Runtime configuration, loaded from the environment / .env via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["anthropic", "openai", "ollama", "mock"]
EmbeddingProvider = Literal["hash", "ollama", "openai"]

REPO_ROOT = Path(__file__).resolve().parents[2]


class ModelPrice(BaseModel):
    """USD per million tokens."""

    input: float
    output: float


# Anthropic first-party list prices (USD / MTok). Local models are free.
DEFAULT_PRICING: dict[str, ModelPrice] = {
    "claude-sonnet-5-5": ModelPrice(input=2.0, output=10.0),
    "claude-haiku-4-5-20251001": ModelPrice(input=1.0, output=5.0),
    "claude-haiku-4-5": ModelPrice(input=1.0, output=5.0),
    "claude-opus-5-5": ModelPrice(input=4.0, output=20.0),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    service_name: str = "prospectpilot"

    database_url: str = (
        "postgresql+psycopg://prospectpilot:prospectpilot@localhost:5432/prospectpilot"
    )

    # LLM
    llm_provider: Provider = "anthropic"
    llm_seed: int = 7
    anthropic_api_key: SecretStr | None = None
    anthropic_large_model: str = "claude-sonnet-5-5"
    anthropic_small_model: str = "claude-haiku-4-5-20251001"
    anthropic_effort: Literal["low", "medium", "high"] = "low"
    anthropic_structured_outputs: bool = True
    anthropic_refusal_fallback: bool = True
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: SecretStr | None = None
    openai_large_model: str = ""
    openai_small_model: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_large_model: str = "qwen2.5"
    ollama_small_model: str = "qwen2.5"
    ollama_num_ctx: int = 8192
    llm_timeout_s: float = 180.0
    llm_max_concurrency: int = 4
    pricing: dict[str, ModelPrice] = Field(default_factory=lambda: dict(DEFAULT_PRICING))

    # embeddings
    embedding_provider: EmbeddingProvider = "hash"
    embedding_dim: int = 768
    ollama_embed_model: str = "nomic-embed-text"
    openai_embed_model: str = "text-embedding-3-small"

    # sources / fetching
    http_user_agent: str = (
        "ProspectPilotBot/0.1 (+https://github.com/AbhilashSomigari/prospectpilot; "
        "B2B research bot; respects robots.txt; 1 req/s per domain)"
    )
    http_cache_dir: Path = Path(".cache/http")
    http_min_interval_s: float = 1.0
    http_timeout_s: float = 15.0
    github_token: SecretStr | None = None

    # api
    api_key: SecretStr | None = None  # when set, endpoints require "Authorization: Bearer <key>"

    # sending
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    mailpit_api_url: str = "http://localhost:8025"
    mail_from: str = "ProspectPilot Demo <sdr@prospectpilot.test>"
    allow_real_send: bool = False
    followup_days: list[int] = Field(default_factory=lambda: [3, 7])

    # pipeline
    min_email_confidence: float = 0.6  # below this a lead is not drafted or contacted
    max_people_per_company: int = 2

    # writer / critic
    email_max_words: int = 120
    min_facts_per_sequence: int = 2
    critic_max_rewrites: int = 2
    critic_min_judge: float = 3.5

    # self-improvement promotion gates
    improve_ci_level: float = 0.95  # paired bootstrap CI of the pass-rate gain must exclude 0
    improve_bootstrap_samples: int = 5000
    improve_grounding_max_drop: float = 0.02  # absolute drop allowed in grounded-claim rate

    # observability
    otel_enabled: bool = True
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    langfuse_enabled: bool = False
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr | None = None
    worker_metrics_port: int = 9101

    # evals
    evals_dir: Path = REPO_ROOT / "evals"

    def price_for(self, model: str) -> ModelPrice:
        return self.pricing.get(model, ModelPrice(input=0.0, output=0.0))

    @property
    def sync_database_url(self) -> str:
        return self.database_url

    @property
    def libpq_database_url(self) -> str:
        """Plain postgresql:// URL for psycopg/langgraph (no SQLAlchemy driver suffix)."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
