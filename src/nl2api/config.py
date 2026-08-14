"""Application settings.

Every tunable lives here, is read from the environment exactly once, and is
typed. Nothing else in the codebase reads ``os.environ`` directly.

Guardrail thresholds are configuration rather than constants on purpose: the
approval ceiling and write-step cap are policy decisions an operator should be
able to change without editing code.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Role(StrEnum):
    """Caller roles, ordered from least to most privileged.

    Privilege is cumulative: ``billing_admin`` implies ``support_agent`` implies
    ``viewer``. :meth:`implies` is the only place that ordering is encoded.
    """

    VIEWER = "viewer"
    SUPPORT_AGENT = "support_agent"
    BILLING_ADMIN = "billing_admin"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]

    def implies(self, required: Role) -> bool:
        """True if a caller holding this role satisfies ``required``."""
        return self.rank >= required.rank


_ROLE_RANK: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.SUPPORT_AGENT: 1,
    Role.BILLING_ADMIN: 2,
}


LLMProvider = Literal["anthropic", "cassette", "rules"]


class Settings(BaseSettings):
    """Runtime configuration, populated from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="NL2API_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- LLM ---------------------------------------------------------------
    # Unprefixed because every Anthropic tool expects this exact name.
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    model: str = "claude-haiku-4-5"
    llm_provider: LLMProvider = "anthropic"
    llm_max_tokens: int = Field(default=4096, ge=256, le=64_000)

    # -- Services ----------------------------------------------------------
    mock_api_url: str = "http://127.0.0.1:8000"
    assistant_url: str = "http://127.0.0.1:8001"
    request_timeout_seconds: float = Field(default=15.0, gt=0)

    # -- Storage -----------------------------------------------------------
    database_url: str = "sqlite:///./nl2api.db"

    # -- Guardrails --------------------------------------------------------
    default_role: Role = Role.SUPPORT_AGENT
    # A hard ceiling, not an approval threshold. Refunds are *already* always
    # approval-gated by their risk level, so "escalate to approval above X"
    # would be a rule that never fires. Above this amount the assistant will not
    # propose a refund at all — a human uses the admin console directly.
    refund_max_cents: int = Field(default=50_000, ge=0)
    max_write_steps: int = Field(default=3, ge=1, le=20)
    retriever_top_k: int = Field(default=6, ge=1, le=50)

    # -- Logging -----------------------------------------------------------
    log_level: str = "INFO"

    @field_validator("mock_api_url", "assistant_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return level

    @property
    def has_llm_credentials(self) -> bool:
        """Whether a live provider call is possible.

        The planner consults this to decide whether to fall back to the
        offline (cassette / rule-based) path, which is how the test suite runs
        without network access.
        """
        key = self.anthropic_api_key
        return key is not None and bool(key.get_secret_value().strip())

    @property
    def effective_llm_provider(self) -> LLMProvider:
        """The provider we can actually use, not just the one requested."""
        if self.llm_provider == "anthropic" and not self.has_llm_credentials:
            return "rules"
        return self.llm_provider


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that a request path never re-reads ``.env``. Tests that need a
    different configuration call ``get_settings.cache_clear()`` after patching
    the environment.
    """
    return Settings()
