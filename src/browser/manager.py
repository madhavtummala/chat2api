"""Playwright lifecycle and a small pool of reusable browser tabs.

The manager owns a single *persistent* Chromium context (so that a manual or
scripted login survives restarts) and hands out :class:`PageLease` objects. A
bounded pool serialises access to tabs, which is essential because a chat UI
can only process one in-flight turn per tab.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from ..config import Settings

logger = logging.getLogger(__name__)


class PageLease:
    """An exclusive borrow of a pooled :class:`~playwright.async_api.Page`."""

    def __init__(self, page: Page):
        self.page = page


class BrowserManager:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._pool: asyncio.Queue[Page] | None = None
        self._pages: list[Page] = []
        self._uses: dict[Page, int] = {}
        self._replace_tasks: set[asyncio.Task] = set()
        self._started = False
        self._start_lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            logger.info("Starting Playwright (headless=%s)", self._settings.headless)
            self._playwright = await async_playwright().start()

            launch_args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
            if self._settings.storage_state:
                # Ephemeral context seeded from an exported storage_state file.
                self._browser = await self._playwright.chromium.launch(
                    headless=self._settings.headless, args=launch_args
                )
                self._context = await self._browser.new_context(
                    storage_state=self._settings.storage_state
                )
            else:
                # Persistent context: cookies/localStorage live in user_data_dir.
                user_data_dir = Path(self._settings.user_data_dir).resolve()
                user_data_dir.mkdir(parents=True, exist_ok=True)
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir),
                    headless=self._settings.headless,
                    args=launch_args,
                )

            self._context.set_default_timeout(self._settings.nav_timeout_ms)

            self._pool = asyncio.Queue()
            for _ in range(self._settings.max_concurrency):
                page = await self._context.new_page()
                self._pages.append(page)
                self._uses[page] = 0
                self._pool.put_nowait(page)

            self._started = True
            logger.info("Browser ready with %d tab(s)", len(self._pages))

    async def stop(self) -> None:
        async with self._start_lock:
            if not self._started:
                return
            logger.info("Shutting down browser")
            try:
                if self._context:
                    await self._context.close()
                if self._browser:
                    await self._browser.close()
                if self._playwright:
                    await self._playwright.stop()
            finally:
                for task in self._replace_tasks:
                    task.cancel()
                self._replace_tasks.clear()
                self._playwright = self._browser = self._context = None
                self._pool = None
                self._pages.clear()
                self._uses.clear()
                self._started = False

    @property
    def context(self) -> BrowserContext:
        if not self._context:
            raise RuntimeError("BrowserManager not started")
        return self._context

    # -- tab checkout ------------------------------------------------------
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[PageLease]:
        """Borrow a pooled tab for the duration of the block.

        The tab is always returned to the pool. It is *recycled* (closed and
        replaced with a fresh one) if the block raised, if the page died, or if
        it has exceeded ``max_tab_uses`` — so a wedged tab never re-enters
        rotation and long-lived tabs don't accumulate cruft. The pool size is
        invariant, so concurrency stays capped at ``max_concurrency``.
        """
        if not self._started or self._pool is None:
            await self.start()
        assert self._pool is not None
        page = await self._pool.get()
        failed = False
        try:
            yield PageLease(page)
        except BaseException:
            failed = True
            raise
        finally:
            self._release(page, failed)

    def _release(self, page: Page, failed: bool) -> None:
        """Return a healthy tab to the pool, or replenish a broken one.

        This is **synchronous** so it always completes — even when the request
        is being cancelled (client abort) mid-``finally``. A tab that needs
        recycling is replaced in a *detached* task, so an aborted request can
        never shrink the pool and leave later requests waiting on an empty pool.
        """
        assert self._pool is not None
        uses = self._uses.get(page, 0) + 1
        cap = self._settings.max_tab_uses
        exhausted = bool(cap) and uses >= cap
        if not (failed or exhausted or page.is_closed()):
            self._uses[page] = uses
            self._pool.put_nowait(page)
            return

        reason = "error" if failed else ("use-cap" if exhausted else "closed")
        self._uses.pop(page, None)
        if page in self._pages:
            self._pages.remove(page)
        task = asyncio.create_task(self._replace(page, reason))
        self._replace_tasks.add(task)
        task.add_done_callback(self._replace_tasks.discard)

    async def _replace(self, page: Page, reason: str) -> None:
        """Close a spent tab and add a fresh one, keeping the pool size constant."""
        logger.info("Recycling tab (reason=%s)", reason)
        try:
            if not page.is_closed():
                await page.close()
        except Exception:  # noqa: BLE001 - closing a dead page is best-effort
            logger.debug("Error closing recycled tab", exc_info=True)
        if not self._started or self._context is None or self._pool is None:
            return  # shutting down
        try:
            fresh = await self._context.new_page()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to create replacement tab")
            return
        self._pages.append(fresh)
        self._uses[fresh] = 0
        self._pool.put_nowait(fresh)
