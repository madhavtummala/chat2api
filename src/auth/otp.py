"""One-time-code retrieval for unattended re-authentication.

Providers whose session expires on a fixed clock (ExpressAI's is 24h) need a
human at a noVNC session to get back in, because the re-login sends a one-time
code by email. An :class:`OTPSource` is where that code comes from, so the login
choreography in :mod:`.login` never has to know.

The only implementation today reads Gmail over its REST API. It talks to the
endpoints directly with ``httpx`` rather than pulling in
``google-api-python-client``, which drags ~50MB of transitive dependencies onto
a Raspberry Pi to perform two GETs.

**Scope warning.** The grant is ``gmail.readonly``, which is the least Google
offers that can read a message body — there is no per-label or per-sender scope.
The filtering in the query below narrows what *we* ask for, not what the token
can reach: anyone holding it can read the whole mailbox. Point this at a
dedicated account that receives nothing but forwarded one-time codes; see
`docs/auto-login.md`.

Nothing here writes. Single-use is enforced by the watermark — a code that
predates the current login attempt is rejected — so the mailbox is never
modified to track what has been consumed.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

import httpx

from ..config import Settings
from ..core.errors import ProviderError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OTPCriteria:
    """How to recognise *one provider's* code mail.

    Deliberately not global settings: a sender address and a code format belong
    to a provider in the same way its CSS selectors do, so two providers needing
    auto-login can't collide over one shared pattern. The mailbox itself (which
    account, which credentials) stays global — that really is one per install.
    """

    #: Sender to restrict the search to, e.g. ``info@info.expressvpn.com``.
    sender: str = ""
    #: Regex locating the code. Group 1 if it captures, else the whole match.
    pattern: str = r"(?i)code\D{0,20}(\d{4,8})"
    #: Optional mailbox label to narrow to (deployment-level, not provider).
    label: str = ""

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
# Refresh a little before the access token actually lapses, so a token that
# expires mid-request doesn't turn into a spurious 401.
_TOKEN_EXPIRY_MARGIN_S = 60.0


class OTPUnavailable(ProviderError):
    """No one-time code could be obtained (misconfigured, or none arrived)."""

    error_type = "otp_unavailable"


class OTPSource(Protocol):
    """Somewhere one-time codes arrive."""

    async def watermark(self) -> float:
        """Epoch seconds to treat as "now" — codes at or before this are stale.

        Taken *before* the login is triggered. Without it the first poll happily
        finds the previous login's code, submits an already-used value, and the
        failure looks like a broken selector rather than a race.
        """

    async def wait_for_code(
        self, since: float, timeout: float, criteria: OTPCriteria
    ) -> str:
        """Poll until a code newer than ``since`` arrives. Raise on timeout."""


def build_otp_source(settings: Settings) -> OTPSource | None:
    """The configured source, or None when auto-login is switched off."""
    kind = (settings.otp_source or "").strip().lower()
    if not kind or kind == "none":
        return None
    if kind == "gmail":
        return GmailOTPSource(settings)
    raise OTPUnavailable(
        f"Unknown CHAT2API_OTP_SOURCE {kind!r} (supported: 'gmail', or empty to disable)."
    )


class GmailOTPSource:
    """Reads one-time codes from a Gmail mailbox via the REST API.

    Freshness is decided on each message's ``internalDate`` rather than Gmail's
    own ``after:``/``newer_than:`` query terms, whose granularity is far too
    coarse (days) to distinguish this login's code from the last one's.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        missing = [
            name
            for name, value in (
                ("CHAT2API_GMAIL_CLIENT_ID", settings.gmail_client_id),
                ("CHAT2API_GMAIL_CLIENT_SECRET", settings.gmail_client_secret),
                ("CHAT2API_GMAIL_REFRESH_TOKEN", settings.gmail_refresh_token),
            )
            if not value
        ]
        if missing:
            raise OTPUnavailable(
                "CHAT2API_OTP_SOURCE=gmail needs "
                + ", ".join(missing)
                + ". Run `python scripts/gmail_oauth.py` once to mint a refresh token."
            )

    # -- OTPSource ---------------------------------------------------------
    async def watermark(self) -> float:
        return time.time()

    async def wait_for_code(
        self, since: float, timeout: float, criteria: OTPCriteria
    ) -> str:
        """Poll the mailbox until a message newer than ``since`` yields a code.

        Mail delivery lags the login click by anything from a second to most of
        a minute, so this polls rather than looking once.
        """
        deadline = time.monotonic() + timeout
        interval = self.settings.otp_poll_interval_s
        attempt = 0
        while True:
            try:
                code = await self._find_code(since, criteria)
            except OTPUnavailable:
                raise  # configuration/credential failures are not worth retrying
            except Exception as exc:  # noqa: BLE001 - a blip shouldn't end the wait
                logger.warning("Gmail poll failed (%s); retrying", exc)
                code = None
            if code:
                return code
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OTPUnavailable(
                    f"No one-time code arrived within {timeout:.0f}s using "
                    f"{self._query(criteria)!r}. Check the sender is right and that "
                    f"the pattern {criteria.pattern!r} matches the message."
                )
            # Back off gently: the code usually lands in the first few seconds,
            # and hammering the API for a minute afterwards achieves nothing.
            attempt += 1
            await asyncio.sleep(min(interval * min(attempt, 4), remaining))

    # -- Gmail plumbing ----------------------------------------------------
    async def _find_code(self, since: float, criteria: OTPCriteria) -> str | None:
        """The newest code strictly newer than ``since``, if any."""
        since_ms = since * 1000.0
        async with httpx.AsyncClient(timeout=self.settings.otp_http_timeout_s) as client:
            headers = {"Authorization": f"Bearer {await self._token(client)}"}
            listing = await self._get(
                client,
                f"{_GMAIL_API}/messages",
                headers,
                params={"q": self._query(criteria), "maxResults": 10},
            )
            for stub in listing.get("messages") or []:
                message = await self._get(
                    client,
                    f"{_GMAIL_API}/messages/{stub['id']}",
                    headers,
                    params={"format": "full"},
                )
                if float(message.get("internalDate", 0)) <= since_ms:
                    continue  # predates this login attempt: already used or unrelated
                code = self._extract(message, criteria.pattern)
                if code:
                    return code
        return None

    def _query(self, criteria: OTPCriteria) -> str:
        # `newer_than:1d` is only a cheap server-side bound to keep the listing
        # small; the real freshness test is internalDate vs the watermark.
        # Deliberately *not* `is:unread` — opening the mail yourself would then
        # hide the code from us, and read state guarantees nothing anyway.
        parts = ["newer_than:1d"]
        if criteria.label.strip():
            parts.insert(0, f'label:"{criteria.label.strip()}"')
        if criteria.sender.strip():
            parts.insert(0, f"from:{criteria.sender.strip()}")
        return " ".join(parts)

    def _extract(self, message: dict[str, Any], pattern_src: str) -> str | None:
        """Pull the code out of a message's subject and body."""
        pattern = re.compile(pattern_src)
        for text in self._texts(message):
            match = pattern.search(text)
            if match:
                # Group 1 when the pattern brackets the code, else the whole match.
                return (match.group(1) if match.groups() else match.group(0)).strip()
        return None

    def _texts(self, message: dict[str, Any]) -> Iterator[str]:
        """Subject first, then every decoded text part.

        Subject leads because providers routinely put the code there, and a
        subject line has none of the tracking-pixel and style noise that makes
        an HTML body throw false positives at a bare ``\\d{6}`` pattern.
        """
        payload = message.get("payload") or {}
        for header in payload.get("headers") or []:
            if header.get("name", "").lower() == "subject":
                yield header.get("value", "")
                break
        yield from self._parts(payload)

    def _parts(self, part: dict[str, Any]) -> Iterator[str]:
        """Decoded text from ``part`` and its children, plain text before HTML."""
        children = part.get("parts") or []
        if children:
            ordered = sorted(children, key=lambda p: p.get("mimeType") != "text/plain")
            for child in ordered:
                yield from self._parts(child)
            return
        if not (part.get("mimeType") or "").startswith("text/"):
            return
        data = (part.get("body") or {}).get("data")
        if not data:
            return
        try:
            # Gmail strips base64 padding; "==" is always safe to add back.
            yield base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - one unreadable part isn't fatal
            logger.debug("Undecodable message part", exc_info=True)

    async def _token(self, client: httpx.AsyncClient) -> str:
        """A valid access token, refreshed from the long-lived refresh token."""
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token
        response = await client.post(
            _TOKEN_URL,
            data={
                "client_id": self.settings.gmail_client_id,
                "client_secret": self.settings.gmail_client_secret,
                "refresh_token": self.settings.gmail_refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code >= 400:
            # A revoked or expired refresh token stays broken until a human
            # re-consents, so say so rather than letting it look transient.
            raise OTPUnavailable(
                f"Gmail token refresh failed ({response.status_code}): {response.text[:200]}. "
                "Re-run `python scripts/gmail_oauth.py` if the grant was revoked."
            )
        payload = response.json()
        self._access_token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 3600))
        self._token_expires_at = time.monotonic() + max(expires_in - _TOKEN_EXPIRY_MARGIN_S, 0)
        return self._access_token

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        response = await client.get(url, headers=headers, params=params)
        if response.status_code == 401:
            self._access_token = None  # force a refresh on the next attempt
        response.raise_for_status()
        return response.json()
