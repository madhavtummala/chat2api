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
    # Chromium keeps *session* cookies (the ones with no expiry) in memory only
    # and discards them on close, and some sites keep their SSO identity there —
    # ExpressAI's `bff_session` is a persistent 7-day cookie, but it can only be
    # renewed silently while the ExpressVPN Keycloak session cookies are still
    # around. Losing those on every restart turns a routine renewal into a
    # re-login. Saving them out and re-adding them with a real expiry on launch
    # is what keeps a login alive between restarts.
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

    # ---- Unattended re-authentication -----------------------------------
    # Drive a provider's sign-in form when its session expires, instead of
    # requiring a human at the noVNC session. Off by default: it needs stored
    # credentials and a mailbox to read one-time codes from.
    auto_login: bool = False
    # Where one-time codes are read from: "gmail", or empty to disable. A
    # provider whose login demands a code cannot auto-login without this.
    otp_source: str = ""
    # OAuth client + refresh token for the mailbox. Mint with
    # `python scripts/gmail_oauth.py`. NOTE: the token grants read access to the
    # *whole* mailbox — the label below narrows our query, not the token's
    # power — so point this at a dedicated account that receives only forwarded
    # one-time codes. See docs/auto-login.md.
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    # Optional mailbox label to narrow the search to. Empty (the default) is
    # right for everyone who hasn't deliberately created one: the provider
    # already restricts the query to its own sender, so a label adds nothing —
    # and a label that doesn't exist matches no mail at all, surfacing as a
    # baffling "no one-time code arrived".
    gmail_otp_label: str = ""
    # Fallback code regex for a provider whose LoginFlow doesn't specify one.
    # The sender and the per-provider pattern live on the provider's LoginFlow,
    # not here — they describe one site, exactly like its CSS selectors, and two
    # providers needing auto-login would otherwise collide over one setting.
    # Override per provider with CHAT2API_<PROVIDER>_OTP_FROM / _OTP_PATTERN.
    otp_code_pattern: str = r"(?i)code\D{0,20}(\d{4,8})"
    # How long to wait for the code mail to arrive after the form is submitted.
    otp_wait_timeout_s: float = Field(default=120.0, gt=0)
    # Delay between mailbox polls (grows up to 4x while waiting).
    otp_poll_interval_s: float = Field(default=3.0, gt=0)
    otp_http_timeout_s: float = Field(default=30.0, gt=0)
    # Per-step element wait during login. Deliberately shorter than
    # nav_timeout_ms: most steps are optional, and a missing one should be
    # skipped promptly rather than stalling the whole login.
    login_step_timeout_ms: int = Field(default=10_000, gt=0)
    # How long to refuse further auto-login attempts after one fails. Each
    # attempt spends a single-use code and counts against the provider's
    # rate limit, so a broken login must not be retried per request.
    login_retry_cooldown_s: float = Field(default=900.0, ge=0)

    # ---- Provider-specific: ExpressAI -----------------------------------
    expressai_base_url: str = "https://app.expressai.com"
    # Account email for auto-login (unused unless CHAT2API_AUTO_LOGIN=true).
    expressai_email: str = ""
    # Unused by the default flow: ExpressVPN's Keycloak signs in with an email
    # and an emailed code. Kept for its alternative password route.
    expressai_password: str = ""
    # Optional overrides for the code mail, if ExpressVPN ever changes them.
    # Empty means "use what providers/expressai.py declares".
    expressai_otp_from: str = ""
    expressai_otp_pattern: str = ""
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
