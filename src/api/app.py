"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..browser import BrowserManager
from ..config import Settings, settings
from ..mcp_bridge import McpManager, load_specs
from ..providers import ProviderRouter, available_providers
from .responses_routes import router as responses_router
from .routes import router
from .sessions import SessionStore

logger = logging.getLogger(__name__)


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
        try:
            await browser.start()
            # Warm only the default provider so /health has a login state at
            # boot; other providers warm lazily on first request.
            default = provider_router.get(provider_router.default_name)
            await default.startup()
            await default.refresh_models()
            await mcp.startup()
        except Exception:
            logger.exception("Startup failed; server will report unhealthy")
        try:
            yield
        finally:
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
