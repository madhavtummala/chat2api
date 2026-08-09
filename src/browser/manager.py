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
from ..core.errors import ProviderUnavailable

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
        self._teardown_tasks: set[asyncio.Task] = set()
        self._started = False
        self._closing = False  # True only during an intentional stop()
        self._start_failed = False
        self._start_lock = asyncio.Lock()
        self._pool_lock = asyncio.Lock()

    # -- health ------------------------------------------------------------
    @property
    def is_alive(self) -> bool:
        """Whether a usable browser context is currently up."""
        return self._started and self._context is not None

    @property
    def healthy(self) -> bool:
        """Whether the *service* is healthy, which is not the same as alive.

        A browser that died is recoverable: the next request (or the watchdog)
        relaunches it. Only a browser that is down *and* whose last relaunch
        attempt failed is a real outage worth restarting the container over.
        """
        return self.is_alive or not self._start_failed

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            attempts = max(self._settings.browser_start_attempts, 1)
            for attempt in range(1, attempts + 1):
                try:
                    await self._launch()
                    self._start_failed = False
                    return
                except Exception:
                    # A partial start (e.g. no X server) would otherwise leave a
                    # live Playwright driver behind; every retry would spawn
                    # another and pile up dead Chromium processes. Tear the
                    # partial state down so the next attempt starts clean.
                    await self._abort_partial_start()
                    if attempt == attempts:
                        self._start_failed = True
                        raise
                    delay = 2 ** (attempt - 1)
                    logger.warning(
                        "Browser launch attempt %d/%d failed; retrying in %ds",
                        attempt, attempts, delay, exc_info=True,
                    )
                    await asyncio.sleep(delay)

    async def _launch(self) -> None:
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
        # Chromium can die long after a clean start (OOM kill, renderer crash,
        # X server going away). Without this the manager would keep believing it
        # is started and hand out dead tabs forever; instead we drop all state so
        # the next acquire()/watchdog tick relaunches from scratch.
        self._context.on("close", self._on_context_lost)
        self._started = True
        logger.info("Browser ready (tab pools created lazily per provider)")

    def _on_context_lost(self, _context: BrowserContext) -> None:
        """Handle an *unexpected* context close. Synchronous by necessity.

        Everything that makes the manager look started is cleared here, in one
        synchronous step, so a concurrent ``start()`` can never observe a
        half-reset manager. The stale Playwright objects are captured into
        locals first and disposed of in a detached task — that way disposal can
        never touch the *new* context a relaunch may have installed meanwhile.
        """
        if self._closing or not self._started:
            return  # expected close from stop(), or already reset
        logger.error("Browser context closed unexpectedly; will relaunch on next use")
        stale_playwright, stale_browser = self._playwright, self._browser
        self._playwright = self._browser = self._context = None
        self._started = False
        self._pools.clear()
        self._pool_pages.clear()
        self._page_pool.clear()
        self._uses.clear()
        for task in self._replace_tasks:
            task.cancel()
        self._replace_tasks.clear()
        if stale_playwright is not None or stale_browser is not None:
            self._spawn_teardown(stale_playwright, stale_browser)

    def _spawn_teardown(self, playwright: Playwright | None, browser: Browser | None) -> None:
        async def _dispose() -> None:
            for closer in (
                getattr(browser, "close", None),
                getattr(playwright, "stop", None),
            ):
                if closer is None:
                    continue
                try:
                    await closer()
                except Exception:  # noqa: BLE001 - disposing a dead browser is best-effort
                    logger.debug("Error disposing stale browser objects", exc_info=True)

        task = asyncio.create_task(_dispose())
        self._teardown_tasks.add(task)
        task.add_done_callback(self._teardown_tasks.discard)

    async def _abort_partial_start(self) -> None:
        """Best-effort teardown of a half-initialised start(), resetting state."""
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
            getattr(self._playwright, "stop", None),
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:  # noqa: BLE001 - teardown of a broken start is best-effort
                logger.debug("Error tearing down partial browser start", exc_info=True)
        self._playwright = self._browser = self._context = None
        self._started = False

    async def stop(self) -> None:
        async with self._start_lock:
            if not self._started:
                return
            logger.info("Shutting down browser")
            self._closing = True  # this close is expected; don't trip the watchdog
            try:
                if self._context:
                    await self._context.close()
                if self._browser:
                    await self._browser.close()
                if self._playwright:
                    await self._playwright.stop()
            finally:
                for task in (*self._replace_tasks, *self._teardown_tasks):
                    task.cancel()
                self._replace_tasks.clear()
                self._teardown_tasks.clear()
                self._playwright = self._browser = self._context = None
                self._pools.clear()
                self._pool_pages.clear()
                self._page_pool.clear()
                self._uses.clear()
                self._started = False
                self._closing = False

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
            try:
                for _ in range(self._settings.max_concurrency):
                    page = await self._context.new_page()
                    self._uses[page] = 0
                    self._page_pool[page] = key
                    pages.append(page)
                    pool.put_nowait(page)
            except BaseException:
                # Half-built pool: the tabs made so far are unreachable (the pool
                # is never published), so close them rather than leak a tab per
                # retry while the browser is misbehaving.
                for page in pages:
                    self._uses.pop(page, None)
                    self._page_pool.pop(page, None)
                    self._spawn_teardown_page(page)
                raise
            self._pool_pages[key] = pages
            self._pools[key] = pool
            logger.info("Warmed pool %r with %d tab(s)", key, len(pages))
            return pool

    def _spawn_teardown_page(self, page: Page) -> None:
        async def _close() -> None:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:  # noqa: BLE001 - best-effort
                logger.debug("Error closing orphaned tab", exc_info=True)

        task = asyncio.create_task(_close())
        self._teardown_tasks.add(task)
        task.add_done_callback(self._teardown_tasks.discard)

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
        try:
            page = await asyncio.wait_for(pool.get(), timeout=self._settings.pool_wait_s)
        except asyncio.TimeoutError as exc:
            # Every tab in this pool is still busy (or wedged). Failing fast is
            # strictly better than hanging: the client can retry, a stalled
            # socket cannot.
            raise ProviderUnavailable(
                f"No free browser tab for {pool_key!r} after "
                f"{self._settings.pool_wait_s:.0f}s; the server is busy."
            ) from exc
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
