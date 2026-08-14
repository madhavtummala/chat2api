"""Unattended re-authentication: reading one-time codes and driving a login."""

import asyncio
import base64
import time

import httpx
import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from src.auth import otp as otp_mod
from src.auth.login import AutoLogin, LoginFlow, LoginNotConfigured
from src.auth.otp import GmailOTPSource, OTPCriteria, OTPUnavailable, build_otp_source
from src.config import Settings
from src.providers.browser_chat import BrowserChatProvider
from src.providers.expressai import ExpressAIProvider
from src.core.errors import AuthenticationRequired


def _settings(**overrides) -> Settings:
    base = dict(
        auto_login=True,
        otp_source="gmail",
        gmail_client_id="id",
        gmail_client_secret="secret",
        gmail_refresh_token="refresh",
        otp_poll_interval_s=0.01,
        login_step_timeout_ms=50,
        nav_timeout_ms=50,
    )
    base.update(overrides)
    return Settings(**base)


CRIT = OTPCriteria(pattern=Settings().otp_code_pattern)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _message(msg_id: str, internal_date_ms: float, *, subject="", plain="", html=""):
    parts = []
    if plain:
        parts.append({"mimeType": "text/plain", "body": {"data": _b64(plain)}})
    if html:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    return {
        "id": msg_id,
        "internalDate": str(int(internal_date_ms)),
        "payload": {"headers": [{"name": "Subject", "value": subject}], "parts": parts},
    }


class FakeGmail:
    """Stands in for Gmail's REST API. Records what was asked of it."""

    def __init__(self, messages):
        self.messages = {m["id"]: m for m in messages}
        self.order = [m["id"] for m in messages]
        self.writes: list[str] = []
        self.token_calls = 0
        self.list_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method != "GET" and "oauth2" not in url:
            self.writes.append(f"{request.method} {url}")
        if url.startswith("https://oauth2.googleapis.com/token"):
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        if url.split("?")[0].endswith("/messages"):
            self.list_calls += 1
            return httpx.Response(200, json={"messages": [{"id": i} for i in self.order]})
        msg_id = url.split("/messages/")[1].split("?")[0]
        return httpx.Response(200, json=self.messages[msg_id])

    def install(self, monkeypatch):
        transport = httpx.MockTransport(self.handler)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(otp_mod.httpx, "AsyncClient", factory)
        return self


# -- code extraction -------------------------------------------------------
async def test_reads_code_from_subject(monkeypatch):
    now = time.time() * 1000
    gmail = FakeGmail([_message("m1", now + 1000, subject="Your code is 483920")])
    gmail.install(monkeypatch)

    source = GmailOTPSource(_settings())
    assert await source.wait_for_code(time.time(), 1, CRIT) == "483920"


async def test_reads_code_from_plain_body(monkeypatch):
    now = time.time() * 1000
    gmail = FakeGmail(
        [_message("m1", now + 1000, subject="Sign-in request", plain="Code: 112233\nExpires soon.")]
    )
    gmail.install(monkeypatch)

    source = GmailOTPSource(_settings())
    assert await source.wait_for_code(time.time(), 1, CRIT) == "112233"


async def test_prefers_plain_text_over_html_noise(monkeypatch):
    """HTML bodies carry tracking ids that a bare \\d{6} would happily match."""
    now = time.time() * 1000
    gmail = FakeGmail(
        [
            _message(
                "m1",
                now + 1000,
                subject="Sign-in request",
                plain="Your code is 777111",
                html='<img src="x?t=999888"><p>Your code is 777111</p>',
            )
        ]
    )
    gmail.install(monkeypatch)

    source = GmailOTPSource(_settings())
    assert await source.wait_for_code(time.time(), 1, CRIT) == "777111"


# The real thing, verbatim from an ExpressVPN sign-in mail.
EXPRESSVPN_SUBJECT = "Your ExpressVPN verification code"
EXPRESSVPN_BODY = """ExpressVPN
Verification code: 801622
Enter this verification code to complete your sign-in. This expires in 10 minutes.
If you didn't request this, you can safely ignore this email. If you're concerned
about your account security, you can change your password here.

Chat
Need help? Chat with Support.
SUPPORT CENTER
PRIVACY POLICY
ExpressVPN
Sent by ExpressVPN
Mill Mall, Suite 6, Wickhams Cay 1, Road Town, Tortola, British Virgin Islands
"""


async def test_reads_a_real_expressvpn_code(monkeypatch):
    now = time.time() * 1000
    gmail = FakeGmail(
        [_message("m1", now + 1000, subject=EXPRESSVPN_SUBJECT, plain=EXPRESSVPN_BODY)]
    )
    gmail.install(monkeypatch)

    source = GmailOTPSource(_settings())  # stock pattern, no per-provider tuning
    assert await source.wait_for_code(time.time(), 1, CRIT) == "801622"


def test_default_pattern_ignores_numbers_not_labelled_as_a_code():
    """A bare \\d{6} would happily match an order number or a tracking id."""
    source = GmailOTPSource(_settings())
    noise = {"payload": {"headers": [], "parts": [
        {"mimeType": "text/plain", "body": {"data": _b64("Order 998877 shipped. Ref 123456.")}}
    ]}}
    assert source._extract(noise, CRIT.pattern) is None


async def test_custom_pattern_uses_capture_group(monkeypatch):
    now = time.time() * 1000
    gmail = FakeGmail([_message("m1", now + 1000, plain="Verification: AB-4821")])
    gmail.install(monkeypatch)

    source = GmailOTPSource(_settings())
    crit = OTPCriteria(pattern=r"Verification: ([A-Z]{2}-\d{4})")
    assert await source.wait_for_code(time.time(), 1, crit) == "AB-4821"


# -- the watermark ---------------------------------------------------------
async def test_ignores_codes_older_than_the_watermark(monkeypatch):
    """The previous login's code must never be replayed into this one."""
    now = time.time()
    gmail = FakeGmail([_message("stale", (now - 300) * 1000, subject="Your code is 111111")])
    gmail.install(monkeypatch)

    source = GmailOTPSource(_settings())
    with pytest.raises(OTPUnavailable, match="No one-time code arrived"):
        await source.wait_for_code(now, 0.1, CRIT)


async def test_accepts_a_code_that_arrives_mid_wait(monkeypatch):
    now = time.time()
    gmail = FakeGmail([])
    gmail.install(monkeypatch)

    async def deliver():
        await asyncio.sleep(0.05)
        fresh = _message("late", (now + 10) * 1000, subject="Your code is 246810")
        gmail.messages["late"] = fresh
        gmail.order.append("late")

    asyncio.ensure_future(deliver())
    source = GmailOTPSource(_settings())
    assert await source.wait_for_code(now, 2, CRIT) == "246810"


async def test_a_consumed_code_is_stale_for_the_next_login(monkeypatch):
    """Single-use is enforced by the watermark, not by mutating the mailbox."""
    first_login = time.time()
    delivered_at = first_login + 10
    gmail = FakeGmail([_message("m1", delivered_at * 1000, subject="Your code is 314159")])
    gmail.install(monkeypatch)
    source = GmailOTPSource(_settings())

    assert await source.wait_for_code(first_login, 1, CRIT) == "314159"

    # A later login takes a fresh watermark; the same message is now too old,
    # even though it is still sitting there unread and unmodified.
    second_login = delivered_at + 1
    with pytest.raises(OTPUnavailable):
        await source.wait_for_code(second_login, 0.1, CRIT)


async def test_the_mailbox_is_never_written_to(monkeypatch):
    """The Gmail grant is read-only; nothing here may attempt a write."""
    now = time.time()
    gmail = FakeGmail([_message("m1", (now + 10) * 1000, subject="Your code is 314159")])
    gmail.install(monkeypatch)

    source = GmailOTPSource(_settings())
    assert await source.wait_for_code(now, 1, CRIT) == "314159"
    assert gmail.writes == []


async def test_a_code_already_read_in_the_inbox_is_still_found(monkeypatch):
    """We must not filter on is:unread — opening the mail would hide the code."""
    now = time.time()
    msg = _message("m1", (now + 10) * 1000, subject="Your code is 271828")
    gmail = FakeGmail([msg])
    gmail.install(monkeypatch)

    source = GmailOTPSource(_settings())
    assert await source.wait_for_code(now, 1, CRIT) == "271828"
    assert "is:unread" not in source._query(CRIT)


async def test_access_token_is_reused_across_polls(monkeypatch):
    now = time.time()
    gmail = FakeGmail([])
    gmail.install(monkeypatch)

    source = GmailOTPSource(_settings())
    with pytest.raises(OTPUnavailable):
        await source.wait_for_code(now, 0.1, CRIT)
    assert gmail.list_calls > 1  # it really did poll more than once
    assert gmail.token_calls == 1


# -- configuration ---------------------------------------------------------
def test_source_disabled_by_default():
    assert build_otp_source(Settings()) is None


def test_unknown_source_is_rejected():
    with pytest.raises(OTPUnavailable, match="Unknown CHAT2API_OTP_SOURCE"):
        build_otp_source(Settings(otp_source="imap"))


def test_missing_gmail_credentials_name_what_is_missing():
    with pytest.raises(OTPUnavailable, match="CHAT2API_GMAIL_REFRESH_TOKEN"):
        build_otp_source(Settings(otp_source="gmail", gmail_client_id="x", gmail_client_secret="y"))


# -- driving the login form ------------------------------------------------
class FakeLocator:
    def __init__(self, page, selector, index=0):
        self.page, self.selector, self.index = page, selector, index

    @property
    def first(self):
        return self

    def nth(self, index):
        return FakeLocator(self.page, self.selector, index)

    async def count(self):
        return self.page.counts.get(self.selector, 1 if self._present() else 0)

    def _present(self):
        return self.selector in self.page.present

    async def wait_for(self, state=None, timeout=None):
        if not self._present():
            raise PlaywrightTimeout(f"{self.selector} not found")

    async def click(self):
        self.page.events.append(("click", self.selector))
        self.page.on_click(self.selector)

    async def fill(self, value):
        self.page.events.append(("fill", self.selector, value))

    async def press(self, key):
        self.page.events.append(("press", self.selector, key))


class FakePage:
    """A Page stub that records interactions and can reveal selectors on click."""

    def __init__(self, present, reveals=None, counts=None):
        self.present = set(present)
        self.reveals = reveals or {}
        self.counts = counts or {}
        self.events = []
        self.url = "https://example.test/"

    def locator(self, selector):
        return FakeLocator(self, selector)

    def on_click(self, selector):
        self.present.update(self.reveals.get(selector, ()))


class StubOTP:
    def __init__(self, code="654321", events=None):
        self.code = code
        self.events = events if events is not None else []

    async def watermark(self):
        self.events.append(("watermark",))
        return 1000.0

    async def wait_for_code(self, since, timeout, criteria):
        self.events.append(("wait_for_code", since))
        self.criteria = criteria
        return self.code


FLOW = LoginFlow(
    start_button="#signin",
    email_input="#email",
    email_submit="#email-next",
    password_input="#password",
    password_submit="#password-next",
    otp_input="#otp",
    otp_submit="#otp-next",
)


def _auto_login(page_settings=None, otp=None, **overrides):
    settings = page_settings or _settings()
    return AutoLogin(
        "fake", FLOW, settings, lambda: otp if otp is not None else StubOTP(), CRIT,
        overrides.get("email", "user@example.test"),
        overrides.get("password", "hunter2"),
    )


async def test_login_fills_the_form_and_submits_the_code():
    page = FakePage(["#signin", "#email", "#email-next", "#password", "#password-next",
                     "#otp", "#otp-next"])
    otp = StubOTP("654321")
    logged_out = [True]

    async def still_out():
        return logged_out[0]

    assert await _auto_login(otp=otp).attempt(page, still_out) is True
    assert ("fill", "#email", "user@example.test") in page.events
    assert ("fill", "#password", "hunter2") in page.events
    assert ("fill", "#otp", "654321") in page.events
    assert ("click", "#otp-next") in page.events


async def test_watermark_is_taken_before_the_password_is_typed():
    """Otherwise the first poll finds the *previous* login's code and fails."""
    page = FakePage(["#signin", "#email", "#email-next", "#password", "#password-next",
                     "#otp", "#otp-next"])
    # Share one timeline so OTP calls and page interactions are directly ordered.
    otp = StubOTP(events=page.events)
    await _auto_login(otp=otp).attempt(page, lambda: _true())

    kinds = [e[0] for e in page.events]
    assert kinds.index("watermark") < kinds.index("fill")
    # ...and the code is only requested after the password has gone in.
    password_at = page.events.index(("fill", "#password", "hunter2"))
    assert password_at < kinds.index("wait_for_code")


async def test_enter_is_pressed_when_a_step_has_no_button():
    page = FakePage(["#email", "#password", "#otp"])  # no submit buttons present
    await _auto_login(otp=StubOTP()).attempt(page, lambda: _true())
    assert ("press", "#email", "Enter") in page.events
    assert ("press", "#password", "Enter") in page.events


async def test_missing_required_field_is_a_clear_error():
    page = FakePage(["#signin"])  # email field never shows up
    with pytest.raises(AuthenticationRequired, match="login field '#email' never appeared"):
        await _auto_login(otp=StubOTP()).attempt(page, lambda: _true())


async def test_otp_field_never_appearing_suggests_a_rejected_password():
    page = FakePage(["#email", "#password"])  # password accepted? no OTP step shown
    with pytest.raises(AuthenticationRequired, match="one-time-code field never appeared"):
        await _auto_login(otp=StubOTP()).attempt(page, lambda: _true())


async def test_split_otp_inputs_are_typed_one_character_each():
    flow = LoginFlow(
        email_input="#email", password_input="#password",
        otp_input=".otp-box", otp_input_is_split=True,
    )
    page = FakePage(["#email", "#password", ".otp-box"], counts={".otp-box": 6})
    auto = AutoLogin("fake", flow, _settings(), lambda: StubOTP("135790"), CRIT, "u@e.test", "pw")
    await auto.attempt(page, lambda: _true())

    typed = [e for e in page.events if e[0] == "fill" and e[1] == ".otp-box"]
    assert [e[2] for e in typed] == list("135790")


# -- guard rails -----------------------------------------------------------
async def test_disabled_auto_login_refuses_with_a_reason():
    auto = AutoLogin("fake", FLOW, _settings(auto_login=False), lambda: StubOTP(), CRIT, "u", "p")
    assert auto.enabled is False
    with pytest.raises(LoginNotConfigured, match="auto-login is disabled"):
        await auto.attempt(FakePage([]), lambda: _true())


async def test_missing_credentials_refuse_with_a_reason():
    auto = AutoLogin("fake", FLOW, _settings(), lambda: StubOTP(), CRIT, "", "")
    assert auto.enabled is False
    with pytest.raises(LoginNotConfigured, match="no CHAT2API_FAKE_EMAIL"):
        await auto.attempt(FakePage([]), lambda: _true())


async def test_missing_otp_source_is_reported_before_typing_a_password():
    page = FakePage(["#email", "#password", "#otp"])
    auto = AutoLogin("fake", FLOW, _settings(), lambda: None, CRIT, "u@e.test", "pw")
    with pytest.raises(LoginNotConfigured, match="no OTP source is configured"):
        await auto.attempt(page, lambda: _true())
    assert not any(e[0] == "fill" for e in page.events)


async def test_failed_login_is_not_retried_immediately():
    """Each attempt spends a single-use code, so a broken form must back off."""
    auto = AutoLogin(
        "fake", FLOW, _settings(login_retry_cooldown_s=900), lambda: StubOTP(), CRIT, "u@e.test", "pw"
    )
    broken = FakePage([])  # nothing at all matches

    with pytest.raises(AuthenticationRequired, match="never appeared"):
        await auto.attempt(broken, lambda: _true())
    with pytest.raises(AuthenticationRequired, match="not retrying"):
        await auto.attempt(broken, lambda: _true())


async def test_cooldown_lapses():
    """With the cooldown elapsed, the form is driven again rather than refused."""
    auto = AutoLogin(
        "fake", FLOW, _settings(login_retry_cooldown_s=0), lambda: StubOTP(), CRIT, "u@e.test", "pw"
    )
    with pytest.raises(AuthenticationRequired, match="never appeared"):
        await auto.attempt(FakePage([]), lambda: _true())

    second = FakePage(["#email", "#password", "#otp"])
    assert await auto.attempt(second, lambda: _true()) is True
    assert ("fill", "#otp", "654321") in second.events


async def test_concurrent_logins_run_once():
    """Two tabs finding a dead session must not race for one single-use code."""
    page = FakePage(["#email", "#password", "#otp"])
    logged_out = [True]
    logins = []

    otp = StubOTP()
    original = otp.wait_for_code

    async def counted(since, timeout, criteria):
        logins.append(since)
        await asyncio.sleep(0.05)  # hold the lock long enough for the other task
        logged_out[0] = False      # the session is live from here on
        return await original(since, timeout, criteria)

    otp.wait_for_code = counted
    auto = AutoLogin("fake", FLOW, _settings(), lambda: otp, CRIT, "u@e.test", "pw")

    async def still_out():
        return logged_out[0]

    results = await asyncio.gather(
        auto.attempt(page, still_out), auto.attempt(page, still_out)
    )
    assert sorted(results) == [False, True]  # one logged in, one found it done
    assert len(logins) == 1


async def _true():
    return True


# -- criteria are per provider, not global --------------------------------
class _Prov(BrowserChatProvider):
    """Minimal provider double: only the criteria plumbing is exercised."""

    def __init__(self, name, flow, settings):
        self.name = name
        self.login_flow = flow
        self.settings = settings

    async def generate(self, request):  # pragma: no cover - never called
        yield ""


def test_two_providers_get_their_own_sender_and_pattern():
    """The whole point: one global sender/pattern couldn't serve both."""
    settings = _settings()
    a = _Prov("alpha", LoginFlow(email_input="#e", otp_sender="a@a.test",
                                 otp_pattern=r"A-(\d{4})"), settings)
    b = _Prov("beta", LoginFlow(email_input="#e", otp_sender="b@b.test",
                                otp_pattern=r"B-(\d{6})"), settings)

    assert a.otp_criteria().sender == "a@a.test"
    assert b.otp_criteria().sender == "b@b.test"
    assert a.otp_criteria().pattern == r"A-(\d{4})"
    assert b.otp_criteria().pattern == r"B-(\d{6})"


def test_provider_without_a_pattern_falls_back_to_the_global_default():
    settings = _settings()
    p = _Prov("alpha", LoginFlow(email_input="#e", otp_sender="a@a.test"), settings)
    assert p.otp_criteria().pattern == settings.otp_code_pattern


def test_env_override_beats_what_the_provider_declares():
    """For the day a site changes its sender and you'd rather not redeploy."""
    settings = _settings(expressai_otp_from="new@sender.test",
                         expressai_otp_pattern=r"X(\d{8})")
    p = _Prov("expressai", ExpressAIProvider.login_flow, settings)
    assert p.otp_criteria().sender == "new@sender.test"
    assert p.otp_criteria().pattern == r"X(\d{8})"


def test_expressai_declares_its_own_code_mail():
    p = _Prov("expressai", ExpressAIProvider.login_flow, _settings())
    crit = p.otp_criteria()
    assert crit.sender == "info@info.expressvpn.com"
    # And that pattern must read the real mail (see EXPRESSVPN_BODY above).
    source = GmailOTPSource(_settings())
    msg = {"payload": {"headers": [{"name": "Subject", "value": EXPRESSVPN_SUBJECT}],
                       "parts": [{"mimeType": "text/plain",
                                  "body": {"data": _b64(EXPRESSVPN_BODY)}}]}}
    assert source._extract(msg, crit.pattern) == "801622"


# -- passwordless flows (ExpressVPN Keycloak) ------------------------------
PASSWORDLESS = LoginFlow(
    start_button="header button:has-text('Sign in')",
    email_input="#username",
    email_submit="#kc-login",
    otp_input="#otp",
    otp_submit="#kc-otp-login-form button[type='submit']",
)


def _passwordless(otp=None, **overrides):
    return AutoLogin(
        "fake", PASSWORDLESS, overrides.get("settings", _settings()),
        lambda: otp or StubOTP(), CRIT,
        overrides.get("email", "user@example.test"),
        overrides.get("password", ""),
    )


async def test_passwordless_login_needs_no_password():
    """ExpressVPN goes email -> emailed code; there is no password step."""
    auto = _passwordless()
    assert auto.enabled is True
    assert auto.unavailable_reason() is None

    page = FakePage(["header button", "#username", "#kc-login", "#otp",
                     "#kc-otp-login-form button[type='submit']"])
    assert await auto.attempt(page, lambda: _true()) is True
    assert ("fill", "#username", "user@example.test") in page.events
    assert ("click", "#kc-login") in page.events
    assert ("fill", "#otp", "654321") in page.events
    # Nothing password-shaped was ever typed.
    assert not any("password" in str(e).lower() for e in page.events)


async def test_password_still_required_when_a_flow_declares_one():
    """The optionality must not silently skip a real password step."""
    auto = AutoLogin("fake", FLOW, _settings(), lambda: StubOTP(), CRIT, "u@e.test", "")
    assert auto.enabled is False
    with pytest.raises(LoginNotConfigured, match="no CHAT2API_FAKE_PASSWORD"):
        await auto.attempt(FakePage([]), lambda: _true())


async def test_missing_email_is_reported_on_a_passwordless_flow():
    auto = _passwordless(email="")
    assert auto.enabled is False
    with pytest.raises(LoginNotConfigured, match="no CHAT2API_FAKE_EMAIL"):
        await auto.attempt(FakePage([]), lambda: _true())


async def test_expressai_flow_is_passwordless_and_uses_keycloak_ids():
    flow = ExpressAIProvider.login_flow
    assert flow.password_input == ""      # passwordless by design
    assert flow.email_input == "#username"
    assert flow.email_submit == "#kc-login"
    assert flow.otp_input == "#otp"
    assert flow.configured


def test_expressai_auto_login_enabled_without_a_password():
    settings = _settings(expressai_email="me@example.test")
    p = _Prov("expressai", ExpressAIProvider.login_flow, settings)
    # login_credentials() reads <name>_email / <name>_password by convention.
    auto = AutoLogin("expressai", p.login_flow, settings, lambda: StubOTP(),
                     p.otp_criteria(), settings.expressai_email, settings.expressai_password)
    assert settings.expressai_password == ""
    assert auto.enabled is True
