"""Playwright lifecycle and per-provider pools of reusable browser tabs.

The manager owns a single *persistent* Chromium context (so a manual or scripted
login survives restarts, and one browser can be logged into several sites at
once) and hands out :class:`PageLease` objects. Tabs are partitioned into a
**pool per key** (the provider name): a request for a given provider always
borrows a tab already sitting on that provider's site, so the "clear thread and
resubmit" fast path never has to re-navigate/re-auth another provider's page.
Each pool is a bounded queue that serialises access, which is essential because
a chat UI can only process one in-flight turn per tab.
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
        # One queue of ready tabs per pool key (provider name).
        self._pools: dict[str, asyncio.Queue[Page]] = {}
        self._pool_pages: dict[str, list[Page]] = {}
        self._page_pool: dict[Page, str] = {}  # page -> its pool key (for recycle)
        self._uses: dict[Page, int] = {}
        self._replace_tasks: set[asyncio.Task] = set()
        self._started = False
        self._start_lock = asyncio.Lock()
        self._pool_lock = asyncio.Lock()

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
            self._started = True
            logger.info("Browser ready (tab pools created lazily per provider)")

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
                self._pools.clear()
                self._pool_pages.clear()
                self._page_pool.clear()
                self._uses.clear()
                self._started = False

    @property
    def context(self) -> BrowserContext:
        if not self._context:
            raise RuntimeError("BrowserManager not started")
        return self._context

    # -- pools -------------------------------------------------------------
    async def _ensure_pool(self, key: str) -> asyncio.Queue[Page]:
        """Create (once) a pool of ``max_concurrency`` tabs for ``key``.

        Tabs start blank; the provider's ``_ensure_ready`` navigates them to its
        site on first use, and they stay there for subsequent reuse.
        """
        pool = self._pools.get(key)
        if pool is not None:
            return pool
        async with self._pool_lock:
            pool = self._pools.get(key)
            if pool is not None:
                return pool
            assert self._context is not None
            pool = asyncio.Queue()
            pages: list[Page] = []
            for _ in range(self._settings.max_concurrency):
                page = await self._context.new_page()
                self._uses[page] = 0
                self._page_pool[page] = key
                pages.append(page)
                pool.put_nowait(page)
            self._pool_pages[key] = pages
            self._pools[key] = pool
            logger.info("Warmed pool %r with %d tab(s)", key, len(pages))
            return pool

    # -- tab checkout ------------------------------------------------------
    @asynccontextmanager
    async def acquire(self, pool_key: str = "default") -> AsyncIterator[PageLease]:
        """Borrow a pooled tab for ``pool_key`` for the duration of the block.

        The tab is always returned to *its own* pool. It is *recycled* (closed
        and replaced with a fresh one in the same pool) if the block raised, if
        the page died, or if it exceeded ``max_tab_uses`` — so a wedged tab never
        re-enters rotation and long-lived tabs don't accumulate cruft. Each
        pool's size is invariant, so per-provider concurrency stays capped at
        ``max_concurrency``.
        """
        if not self._started:
            await self.start()
        pool = await self._ensure_pool(pool_key)
        page = await pool.get()
        failed = False
        try:
            yield PageLease(page)
        except BaseException:
            failed = True
            raise
        finally:
            self._release(page, failed)

    def _release(self, page: Page, failed: bool) -> None:
        """Return a healthy tab to its pool, or replenish a broken one.

        This is **synchronous** so it always completes — even when the request
        is being cancelled (client abort) mid-``finally``. A tab that needs
        recycling is replaced in a *detached* task, so an aborted request can
        never shrink a pool and leave later requests waiting on an empty pool.
        """
        key = self._page_pool.get(page)
        pool = self._pools.get(key) if key is not None else None
        if pool is None:  # unknown/already-removed page; nothing to return to
            return
        uses = self._uses.get(page, 0) + 1
        cap = self._settings.max_tab_uses
        exhausted = bool(cap) and uses >= cap
        if not (failed or exhausted or page.is_closed()):
            self._uses[page] = uses
            pool.put_nowait(page)
            return

        reason = "error" if failed else ("use-cap" if exhausted else "closed")
        self._uses.pop(page, None)
        self._page_pool.pop(page, None)
        pages = self._pool_pages.get(key)
        if pages and page in pages:
            pages.remove(page)
        task = asyncio.create_task(self._replace(page, key, reason))
        self._replace_tasks.add(task)
        task.add_done_callback(self._replace_tasks.discard)

    async def _replace(self, page: Page, key: str, reason: str) -> None:
        """Close a spent tab and add a fresh one to the same pool."""
        logger.info("Recycling tab in pool %r (reason=%s)", key, reason)
        try:
            if not page.is_closed():
                await page.close()
        except Exception:  # noqa: BLE001 - closing a dead page is best-effort
            logger.debug("Error closing recycled tab", exc_info=True)
        pool = self._pools.get(key)
        if not self._started or self._context is None or pool is None:
            return  # shutting down / pool gone
        try:
            fresh = await self._context.new_page()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to create replacement tab")
            return
        self._uses[fresh] = 0
        self._page_pool[fresh] = key
        self._pool_pages.setdefault(key, []).append(fresh)
        pool.put_nowait(fresh)
