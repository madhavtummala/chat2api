"""Application configuration.

All settings are read from environment variables (optionally a `.env` file)
using the ``CHAT2API_`` prefix, e.g. ``CHAT2API_HEADLESS=false``.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CHAT2API_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- HTTP server -----------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 9000
    log_level: str = "info"

    # Comma-separated list of API keys accepted as `Authorization: Bearer <key>`.
    # When empty, authentication is disabled (useful for local development).
    api_keys: str = ""

    # ---- Provider selection ---------------------------------------------
    # Which registered provider backs this server (see src/providers/registry.py).
    provider: str = "expressai"

    # ---- MCP -------------------------------------------------------------
    # Path to a JSON file describing MCP servers: {"servers": [{"label":...}]}.
    # Their tools are advertised to the model via prompt injection. Defaults to
    # `mcp.json` in the CWD; auto-loaded when present, ignored when absent.
    mcp_config_path: str | None = "mcp.json"

    # ---- Responses API ---------------------------------------------------
    # Expose the stateful /v1/responses endpoint (agentic loop + MCP execution).
    enable_responses: bool = True
    # Max model<->tool round-trips within a single /v1/responses request.
    max_agent_turns: int = Field(default=6, ge=1)

    # ---- Browser ---------------------------------------------------------
    headless: bool = True
    # Directory used as the Chromium user-data-dir so that logins/cookies
    # persist across restarts. Relative paths resolve against the CWD.
    user_data_dir: str = ".browser_profile"
    # Optional Playwright storage_state JSON path (alternative to user_data_dir
    # for injecting an already-authenticated session).
    storage_state: str | None = None
    # Maximum number of concurrent browser tabs used to serve requests.
    max_concurrency: int = Field(default=2, ge=1)
    # Recycle a pooled tab after this many uses to shed accumulated memory/DOM
    # cruft (0 = never recycle on use count). Tabs are always recycled on error.
    max_tab_uses: int = Field(default=200, ge=0)
    # Per-request navigation / element timeout in milliseconds.
    nav_timeout_ms: int = 45_000

    # ---- Provider-specific: ExpressAI -----------------------------------
    expressai_base_url: str = "https://app.expressai.com"
    # ---- Provider-specific: Perplexity ----------------------------------
    perplexity_base_url: str = "https://www.perplexity.ai"
    # How long to wait (seconds) for a full model response before giving up.
    response_timeout_s: float = 180.0
    # DOM polling interval (seconds) while streaming a response.
    poll_interval_s: float = 0.2

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


settings = Settings()
