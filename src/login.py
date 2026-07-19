"""One-time interactive login helper.

Opens the selected provider's site in a *headful* Chromium using the same
persistent ``user_data_dir`` the server uses, so a manual login is written to
the profile volume. Intended to run inside the ``login`` container (reachable
through noVNC) on a headless server, but works locally too.

    CHAT2API_HEADLESS=false python -m src.login

Log in via the browser window, then stop the process (Ctrl-C, or
``docker compose stop login``). The profile is flushed to disk on shutdown.

Note: Chromium locks a ``user_data_dir`` while in use, so the server must not
be running against the same profile volume at the same time.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from .browser import BrowserManager
from .config import settings
from .providers.registry import create_provider

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if settings.headless:
        logger.warning(
            "CHAT2API_HEADLESS is true — there will be no visible window to log "
            "in with. Set CHAT2API_HEADLESS=false for the login step."
        )
    if settings.storage_state:
        logger.warning(
            "CHAT2API_STORAGE_STATE is set, but this helper writes to the "
            "persistent user-data-dir, not a storage_state file. Unset it here."
        )

    browser = BrowserManager(settings)
    await browser.start()
    provider = create_provider(settings.provider, settings, browser)

    async with browser.acquire() as lease:
        page = lease.page
        url = getattr(provider, "base_url", "") or ""
        if url:
            logger.info("Opening %s", url)
            await page.goto(url, wait_until="domcontentloaded")
        else:
            logger.info(
                "Provider %r has no base_url; navigate to the site manually.",
                settings.provider,
            )

        logger.info(
            "Log in now in the browser (noVNC: http://<host>:6080/vnc.html). "
            "When done, stop this process (Ctrl-C / `docker compose stop login`)."
        )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()

    logger.info("Shutting down; flushing profile to disk…")
    await browser.stop()


if __name__ == "__main__":
    asyncio.run(main())
