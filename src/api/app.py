"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..browser import BrowserManager
from ..config import Settings, settings
from ..core.errors import AuthenticationRequired
from ..mcp_bridge import McpManager, load_specs
from ..providers import ProviderRouter, available_providers
from .responses_routes import router as responses_router
from .routes import router
from .sessions import SessionStore

logger = logging.getLogger(__name__)


async def _watchdog(browser: BrowserManager, interval: float) -> None:
    """Relaunch a browser that died while the server sat idle.

    ``acquire`` already relaunches on demand, but only when a request arrives.
    Without this, a crash during a quiet period stays invisible until the next
    user hits it — and ``/health`` would keep reporting healthy in the meantime,
    since a down-but-recoverable browser is not itself an outage.
    """
    while True:
        await asyncio.sleep(interval)
        if browser.is_alive:
            continue
        logger.warning("Watchdog: browser is down; relaunching")
        try:
            await browser.start()
            logger.info("Watchdog: browser relaunched")
        except Exception:
            # start() has flagged itself unhealthy, so /health now fails and the
            # container restart policy takes over. Nothing more to do here.
            logger.exception("Watchdog: relaunch failed; reporting unhealthy")


def create_app(config: Settings | None = None) -> FastAPI:
    config = config or settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        browser = BrowserManager(config)
        provider_router = ProviderRouter(config, browser)
        logger.info(
            "Booting chat2api: default provider=%s, routable=%s (registered: %s)",
            provider_router.default_name,
            provider_router.enabled,
            available_providers(),
        )
        mcp = McpManager(load_specs(config.mcp_config_path))
        app.state.browser = browser
        app.state.router = provider_router
        app.state.mcp = mcp
        app.state.sessions = SessionStore()
        # A browser we cannot launch at all is not recoverable in place (no X
        # server, Chromium missing, profile unusable) — every request would 500
        # forever behind a container that looks "up". Fail the startup so the
        # restart policy gets a chance to fix it.
        await browser.start()

        # Past this point the browser works, so nothing is fatal. In particular a
        # logged-out provider MUST NOT stop the server: logging in means reaching
        # the live browser over noVNC, which requires the container to stay up.
        try:
            # Warm only the default provider so /health has a login state at
            # boot; other providers warm lazily on first request.
            default = provider_router.get(provider_router.default_name)
            await default.startup()
            await default.refresh_models()
        except AuthenticationRequired:
            logger.warning(
                "Default provider %r is logged out — serving anyway. Log in via noVNC; "
                "/health reports authenticated=false until you do.",
                provider_router.default_name,
            )
        except Exception:
            logger.exception(
                "Default provider warm-up failed; serving anyway (retried per request)"
            )
        try:
            await mcp.startup()
        except Exception:
            logger.exception("MCP startup failed; continuing without MCP tools")

        watchdog = (
            asyncio.create_task(_watchdog(browser, config.watchdog_interval_s))
            if config.watchdog_interval_s
            else None
        )
        try:
            yield
        finally:
            if watchdog is not None:
                watchdog.cancel()
            try:
                await mcp.shutdown()
            finally:
                try:
                    for provider in app.state.router.all_providers():
                        await provider.shutdown()
                finally:
                    await browser.stop()

    app = FastAPI(
        title="chat2api",
        version="0.1.0",
        summary="OpenAI-compatible API over browser-driven chat UIs.",
        lifespan=lifespan,
    )
    app.include_router(router)
    if config.enable_responses:
        app.include_router(responses_router)
    return app


app = create_app()
