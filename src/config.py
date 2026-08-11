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
    # Comma-separated, **ordered** list of routable providers, e.g.
    # `expressai,perplexity`. The order is the routing preference: the first
    # entry serves requests that don't name a provider, and the rest are the
    # failover order behind it. Empty means "all registered providers".
    # Providers are instantiated lazily and their tabs warmed on first use.
    providers: str = ""

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
    # Chromium discards *session* cookies (the ones with no expiry) when it
    # closes, and some sites keep their SSO identity there — ExpressAI's login
    # is a 7-day `bff_session` that can only be renewed silently while the
    # ExpressVPN Keycloak session cookies are still around. Losing those on
    # every restart turns a routine renewal into a manual noVNC login. Saving
    # them out and re-adding them with a real expiry on launch is what keeps a
    # login alive between restarts.
    persist_session_cookies: bool = True
    # Lifetime given to a restored session cookie. Not a security boundary — the
    # site's own token expiry still governs; this only stops us from re-adding a
    # cookie that has been dead for months.
    session_cookie_ttl_days: float = Field(default=60.0, gt=0)
    # How often to snapshot session cookies while running. A clean shutdown
    # saves them anyway, but an OOM kill or `docker kill` never runs it, so the
    # periodic snapshot is what survives the failure modes that actually happen.
    # 0 disables the timer (shutdown save only).
    session_cookie_save_interval_s: float = Field(default=300.0, ge=0)
    # Maximum number of concurrent browser tabs used to serve requests.
    max_concurrency: int = Field(default=2, ge=1)
    # Recycle a pooled tab after this many uses to shed accumulated memory/DOM
    # cruft (0 = never recycle on use count). Tabs are always recycled on error.
    max_tab_uses: int = Field(default=200, ge=0)
    # Per-request navigation / element timeout in milliseconds.
    nav_timeout_ms: int = 45_000
    # How long a request waits for a free tab before giving up with 503. A wedged
    # tab must not stall every later request behind it indefinitely; a prompt 503
    # is retryable by the client, an open-ended hang is not.
    pool_wait_s: float = Field(default=90.0, gt=0)
    # Attempts (with exponential backoff) to launch Chromium before declaring the
    # browser unhealthy. Absorbs transient launch failures on slow/loaded hosts.
    browser_start_attempts: int = Field(default=3, ge=1)
    # Seconds between watchdog checks that relaunch a browser which died while
    # idle. 0 disables the watchdog.
    watchdog_interval_s: float = Field(default=30.0, ge=0)
    # Retry a failed request once on the next healthy provider (and once more on
    # the same one with a fresh tab). Only applies when the client did not pin a
    # provider via a `provider/model` prefix.
    enable_failover: bool = True

    # ---- Provider-specific: ExpressAI -----------------------------------
    expressai_base_url: str = "https://app.expressai.com"
    # ---- Provider-specific: Google AI Mode ------------------------------
    # The prompt is URL-encoded and appended to this. `udm=50` selects AI Mode.
    googleaimode_search_url: str = "https://www.google.com/search?udm=50&q="
    # ---- Provider-specific: Perplexity ----------------------------------
    perplexity_base_url: str = "https://www.perplexity.ai"
    # How long to wait (seconds) for a full model response before giving up.
    response_timeout_s: float = 180.0
    # DOM polling interval (seconds) while streaming a response.
    poll_interval_s: float = 0.2

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def provider_order(self) -> list[str]:
        """Routable providers in preference order (empty = all registered)."""
        return [p.strip() for p in self.providers.split(",") if p.strip()]


settings = Settings()
