"""Driving a provider's sign-in form so an expired session can renew itself.

The choreography is generic — click sign-in, fill an email, fill a password,
wait for a one-time code, submit it — and only the selectors differ per site, so
a provider describes its form with a :class:`LoginFlow` exactly as it already
describes its chat UI with ``Selectors``.

:class:`AutoLogin` wraps that with the two things a login running unattended
needs and a hand-driven one doesn't:

* **Single-flight.** ``max_concurrency`` defaults to 2 and readiness is checked
  per tab lease, so a dead session is discovered by several tabs at once. Two
  concurrent logins would race for one single-use code and both lose.
* **A cooldown.** One-time codes are single-use and rate-limited at the source.
  A login that fails must not be retried by the very next request, or a wrong
  password quietly burns through the day's code allowance.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from ..config import Settings
from ..core.errors import AuthenticationRequired
from .otp import OTPCriteria, OTPSource, OTPUnavailable

logger = logging.getLogger(__name__)


class LoginNotConfigured(AuthenticationRequired):
    """Auto-login is off, or this provider has no login flow described."""


@dataclass(frozen=True)
class LoginFlow:
    """Selectors for one site's sign-in form.

    Every step is optional: a step whose selector is empty, or whose element
    never appears, is skipped. That covers both the sites that put email and
    password on one page and the ones that split them across two, without
    either needing a bespoke code path.
    """

    #: Clicked on the logged-out chat page to enter the sign-in flow.
    start_button: str = ""
    email_input: str = ""
    email_submit: str = ""          # omit when the email field submits on Enter
    #: Leave empty for a passwordless flow (email -> emailed code), which is
    #: what ExpressVPN's Keycloak does by default.
    password_input: str = ""
    password_submit: str = ""
    #: Where the emailed one-time code is typed. Empty means the site doesn't
    #: use one, and the login completes after the password.
    otp_input: str = ""
    otp_submit: str = ""
    #: Some sites spread the code across several single-character inputs. When
    #: set, the code is typed character-by-character across all matches of
    #: ``otp_input`` instead of into the first one.
    otp_input_is_split: bool = False

    # -- how this provider's code mail looks ------------------------------
    # These belong here, not in global settings, for the same reason the
    # selectors do: they describe one site. Two providers needing auto-login
    # would otherwise fight over a single shared sender/pattern.
    #: Sender of the code mail, e.g. ``info@info.expressvpn.com``.
    otp_sender: str = ""
    #: Regex locating the code; empty falls back to the global default.
    otp_pattern: str = ""

    @property
    def configured(self) -> bool:
        """Whether enough is described here to attempt anything."""
        return bool(self.email_input or self.start_button)


class AutoLogin:
    """Per-provider unattended re-authentication."""

    def __init__(
        self,
        provider_name: str,
        flow: LoginFlow,
        settings: Settings,
        otp_factory: Callable[[], OTPSource | None],
        criteria: OTPCriteria,
        email: str,
        password: str,
    ):
        self.provider_name = provider_name
        self.flow = flow
        self.settings = settings
        self.criteria = criteria
        # A factory, not an instance: building a Gmail source validates
        # credentials, and a typo in those must fail the login that needs them
        # rather than the server's startup.
        self._otp_factory = otp_factory
        self.email = email
        self.password = password
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.auto_login
            and self.flow.configured
            and self.email
            and (self.password or not self.flow.password_input)
        )

    def unavailable_reason(self) -> str | None:
        """Why :meth:`attempt` would refuse, for the human-facing error."""
        if not self.settings.auto_login:
            return "auto-login is disabled (set CHAT2API_AUTO_LOGIN=true)"
        if not self.flow.configured:
            return f"{self.provider_name} has no LoginFlow defined"
        if not self.email:
            return f"no CHAT2API_{self.provider_name.upper()}_EMAIL configured"
        if self.flow.password_input and not self.password:
            return f"no CHAT2API_{self.provider_name.upper()}_PASSWORD configured"
        return None

    async def attempt(
        self, page: Page, still_logged_out: Callable[[], Awaitable[bool]]
    ) -> bool:
        """Log in on ``page``. Returns True if a login was performed.

        ``still_logged_out`` is re-checked after the lock is taken, so tabs that
        queued behind a successful login return immediately instead of logging
        in again on top of a live session.
        """
        if not self.enabled:
            raise LoginNotConfigured(
                f"{self.provider_name} is logged out and auto-login cannot run: "
                f"{self.unavailable_reason()}."
            )
        async with self._lock:
            if not await still_logged_out():
                logger.info("%s: another task restored the session", self.provider_name)
                return False
            remaining = self._blocked_until - time.monotonic()
            if remaining > 0:
                # Surfacing the wait (rather than sleeping through it) keeps the
                # request fast and tells the human that a code was already spent.
                raise AuthenticationRequired(
                    f"{self.provider_name} auto-login failed recently; not retrying for "
                    f"another {remaining:.0f}s to avoid burning one-time codes. "
                    "Check the logs, or log in via noVNC."
                )
            try:
                await self._run(page)
            except Exception:
                self._blocked_until = time.monotonic() + self.settings.login_retry_cooldown_s
                raise
            logger.info("%s: auto-login succeeded", self.provider_name)
            return True

    # -- the form ----------------------------------------------------------
    async def _run(self, page: Page) -> None:
        flow = self.flow
        # Resolved up front so a misconfigured mailbox fails before we type a
        # password, rather than after the site has already sent a code.
        source = self._otp_source() if flow.otp_input else None
        # Taken before anything is clicked: a code that already existed when we
        # started belongs to a previous attempt and must never be accepted.
        watermark = await source.watermark() if source else 0.0

        await self._click(page, flow.start_button)
        await self._fill(page, flow.email_input, self.email, required=True)
        await self._advance(page, flow.email_submit, flow.email_input)
        # Passwordless is a first-class path, not a degraded one: ExpressVPN's
        # Keycloak goes straight from the email to an emailed code, and only
        # offers a password as an alternative link.
        if flow.password_input:
            await self._fill(page, flow.password_input, self.password, required=True)
            await self._advance(page, flow.password_submit, flow.password_input)

        if source is not None:
            await self._submit_otp(page, source, watermark)

    def _otp_source(self) -> OTPSource:
        source = self._otp_factory()
        if source is None:
            raise LoginNotConfigured(
                f"{self.provider_name} asks for an emailed code but no OTP source is "
                "configured (set CHAT2API_OTP_SOURCE=gmail)."
            )
        return source

    async def _submit_otp(self, page: Page, source: OTPSource, watermark: float) -> None:
        # Wait for the field first: reaching it proves the password was accepted,
        # and it means we only spend a code once the site has actually sent one.
        try:
            await page.locator(self.flow.otp_input).first.wait_for(
                state="visible", timeout=self.settings.nav_timeout_ms
            )
        except PlaywrightTimeout as exc:
            cause = (
                "the password may have been rejected, or the login page changed"
                if self.flow.password_input
                else "the email may not be a registered account, or the login page changed"
            )
            raise AuthenticationRequired(
                f"{self.provider_name}: the one-time-code field never appeared — {cause} "
                f"(update LoginFlow in providers/{self.provider_name}.py)."
            ) from exc

        logger.info("%s: waiting for a one-time code", self.provider_name)
        try:
            code = await source.wait_for_code(
                watermark, self.settings.otp_wait_timeout_s, self.criteria
            )
        except OTPUnavailable as exc:
            raise AuthenticationRequired(f"{self.provider_name}: {exc}") from exc

        if self.flow.otp_input_is_split:
            await self._fill_split(page, self.flow.otp_input, code)
        else:
            await self._fill(page, self.flow.otp_input, code, required=True)
        await self._advance(page, self.flow.otp_submit, self.flow.otp_input)

    # -- small helpers -----------------------------------------------------
    async def _advance(self, page: Page, button: str, field: str) -> None:
        """Move to the next step: click the submit button, or press Enter.

        Sites are inconsistent about whether a step has its own button, and the
        button is sometimes disabled until the field validates, so falling back
        to Enter covers both without a per-site branch.
        """
        if not await self._click(page, button):
            await self._enter(page, field)

    async def _click(self, page: Page, selector: str) -> bool:
        """Click ``selector`` if it shows up. False when there was nothing to do."""
        if not selector:
            return False
        try:
            target = page.locator(selector).first
            await target.wait_for(state="visible", timeout=self.settings.login_step_timeout_ms)
            await target.click()
            return True
        except (PlaywrightTimeout, PlaywrightError):
            return False

    async def _fill(self, page: Page, selector: str, value: str, *, required: bool) -> bool:
        if not selector:
            return False
        try:
            field = page.locator(selector).first
            await field.wait_for(state="visible", timeout=self.settings.login_step_timeout_ms)
            await field.fill(value)
            return True
        except (PlaywrightTimeout, PlaywrightError) as exc:
            if required:
                raise AuthenticationRequired(
                    f"{self.provider_name}: login field {selector!r} never appeared; the "
                    f"login page may have changed (update LoginFlow in "
                    f"providers/{self.provider_name}.py)."
                ) from exc
            return False

    async def _fill_split(self, page: Page, selector: str, code: str) -> None:
        """Type ``code`` across one-character-per-box OTP inputs."""
        boxes = page.locator(selector)
        count = await boxes.count()
        if count < len(code):
            # Fall back to the single-field path rather than truncating the code.
            await self._fill(page, selector, code, required=True)
            return
        for index, character in enumerate(code):
            await boxes.nth(index).fill(character)

    async def _enter(self, page: Page, selector: str) -> bool:
        """Press Enter in ``selector`` — the fallback when a form has no button."""
        if not selector:
            return False
        try:
            await page.locator(selector).first.press("Enter")
            return True
        except PlaywrightError:
            return False
