"""Runtime settings, read from environment variables / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

# The ONLY address test emails may ever be sent to. Not configurable on purpose.
ALLOWED_TEST_RECIPIENT = "behta.technicalteam@gmail.com"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # LLM
    llm_provider_setting: str = field(default_factory=lambda: _env("LLM_PROVIDER", "auto"))  # auto|anthropic|openai
    anthropic_model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-opus-5-5"))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-5.5"))
    llm_effort: str = field(default_factory=lambda: _env("LLM_EFFORT", "medium"))
    llm_timeout_s: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_S", "180")))
    llm_refusal_fallbacks: bool = field(default_factory=lambda: _env_bool("LLM_REFUSAL_FALLBACKS", True))

    # Search / web
    search_provider: str = field(default_factory=lambda: _env("SEARCH_PROVIDER", "auto"))  # auto|tavily|duckduckgo
    tavily_api_key: str = field(default_factory=lambda: _env("TAVILY_API_KEY"))
    http_timeout_s: float = field(default_factory=lambda: float(_env("HTTP_TIMEOUT_S", "12")))
    results_per_query: int = field(default_factory=lambda: int(_env("RESULTS_PER_QUERY", "6")))
    search_delay_s: float = field(default_factory=lambda: float(_env("SEARCH_DELAY_S", "1.5")))
    max_pages_per_step: int = field(default_factory=lambda: int(_env("MAX_PAGES_PER_STEP", "12")))
    page_text_chars: int = 12000

    # SMTP
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    smtp_from: str = field(default_factory=lambda: _env("SMTP_FROM"))
    smtp_security: str = field(default_factory=lambda: _env("SMTP_SECURITY", "starttls"))  # starttls|ssl|none
    smtp_timeout_s: float = field(default_factory=lambda: float(_env("SMTP_TIMEOUT_S", "20")))

    runs_dir: str = field(default_factory=lambda: _env("RUNS_DIR", "runs"))

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def llm_provider(self) -> str:
        p = self.llm_provider_setting.lower()
        if p in ("anthropic", "openai"):
            return p
        has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
        return "openai" if os.getenv("OPENAI_API_KEY") and not has_anthropic else "anthropic"

    @property
    def llm_model(self) -> str:
        return self.openai_model if self.llm_provider == "openai" else self.anthropic_model

    @property
    def llm_key_present(self) -> bool:
        if self.llm_provider == "openai":
            return bool(os.getenv("OPENAI_API_KEY"))
        return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
