"""Session-cookie persistence across browser restarts.

Chromium discards *session* cookies (the ones with no expiry) when it closes,
and some sites keep their SSO identity there — ExpressAI's login is a 7-day
`bff_session` that can only be renewed silently while the ExpressVPN Keycloak
session cookies are still around. Losing those on every restart turns a routine
renewal into a manual noVNC login. Saving them out and re-adding them with a
real expiry on launch is what keeps a login alive between restarts.

Everything here is best-effort: persistence is never worth failing a launch or
a request over.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext

from ..config import Settings

logger = logging.getLogger(__name__)

# Keys Playwright accepts back in add_cookies(). Anything else it hands us
# (partitionKey, and whatever a future version adds) is dropped on the way in.
_COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")


def cookie_file(settings: Settings) -> Path:
    return Path(settings.user_data_dir).resolve() / "session_cookies.json"


async def save(context: BrowserContext, settings: Settings) -> None:
    """Snapshot ``context``'s session cookies. Never raises.

    Only cookies *without* an expiry are saved: everything else is already
    durable in the profile, and re-adding a stale copy of a rotating token would
    be actively harmful.
    """
    if not settings.persist_session_cookies:
        return
    try:
        cookies = [c for c in await context.cookies() if not _has_expiry(c)]
        path = cookie_file(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        # These are live credentials: write them user-only, and swap them in
        # atomically so a crash mid-write can't leave a truncated file.
        tmp.write_text(json.dumps([_pick(c) for c in cookies], indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        logger.debug("Saved %d session cookie(s) to %s", len(cookies), path)
    except Exception:  # noqa: BLE001 - persistence is never worth failing over
        logger.warning("Could not save session cookies", exc_info=True)


async def restore(context: BrowserContext, settings: Settings) -> None:
    """Re-add saved session cookies to a fresh ``context``. Never raises.

    They come back with a real expiry, since a cookie re-added as a session
    cookie would be dropped again by the next restart. A cookie the profile
    already carries is left alone — the live one is by definition fresher than
    our snapshot.
    """
    if not settings.persist_session_cookies:
        return
    try:
        raw = cookie_file(settings).read_text()
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("Could not read saved session cookies", exc_info=True)
        return
    try:
        saved = json.loads(raw)
        existing = {_identity(c) for c in await context.cookies()}
        expires = time.time() + settings.session_cookie_ttl_days * 86_400
        fresh = [
            {**_pick(c), "expires": expires}
            for c in saved
            if _identity(c) not in existing
        ]
        if fresh:
            await context.add_cookies(fresh)
        logger.info("Restored %d/%d saved session cookie(s)", len(fresh), len(saved))
    except Exception:  # noqa: BLE001 - a bad snapshot must not block startup
        logger.warning("Could not restore session cookies", exc_info=True)


def _pick(cookie: dict[str, Any]) -> dict[str, Any]:
    """A cookie reduced to the fields Playwright round-trips."""
    return {k: cookie[k] for k in _COOKIE_KEYS if k in cookie}


def _identity(cookie: dict[str, Any]) -> tuple[str, str, str]:
    """What makes two cookies the same cookie, per RFC 6265."""
    return (cookie.get("name", ""), cookie.get("domain", ""), cookie.get("path", ""))


def _has_expiry(cookie: dict[str, Any]) -> bool:
    """Whether the browser will keep this cookie on its own. Session cookies
    come back from Playwright as ``expires: -1``."""
    return cookie.get("expires", -1) > 0
