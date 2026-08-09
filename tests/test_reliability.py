"""Availability behaviour: provider failover, health reporting, browser recovery."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import router
from src.browser.manager import BrowserManager
from src.config import Settings
from src.core.errors import (
    AuthenticationRequired,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)
from src.core.types import ChatRequest
from src.providers.base import BaseChatProvider

from .conftest import FakeProvider


class FailingProvider(FakeProvider):
    """Yields ``deltas`` (possibly none) and then always raises ``error``."""

    def __init__(self, name, error, deltas=None, models=("m",)):
        super().__init__(deltas=deltas or [], error=error)
        self.name = name
        self.default_model = models[0]
        self.available_models = tuple(models)
        self.calls = 0

    async def generate(self, request: ChatRequest):
        self.calls += 1
        for delta in self._deltas:
            yield delta
        raise self._error


class OkProvider(FakeProvider):
    def __init__(self, name, deltas, models=("m",)):
        super().__init__(deltas=deltas)
        self.name = name
        self.default_model = models[0]
        self.available_models = tuple(models)
        self.calls = 0

    async def generate(self, request: ChatRequest):
        self.calls += 1
        self.last_model = request.model
        for delta in self._deltas:
            yield delta


class MultiRouter:
    """ProviderRouter stand-in over several providers, with real failover config."""

    def __init__(self, providers, enable_failover=True):
        self._providers = {p.name: p for p in providers}
        self._default = providers[0].name
        self.settings = Settings(enable_failover=enable_failover)

    @property
    def default_name(self):
        return self._default

    @property
    def enabled(self):
        return list(self._providers)

    def get(self, name):
        return self._providers[name]

    def all_providers(self):
        return list(self._providers.values())

    def split(self, model):
        prefix, sep, rest = model.partition("/")
        if sep and prefix in self._providers:
            return prefix, rest
        return self._default, model


def make_client(providers, enable_failover=True, browser=None):
    app = FastAPI()
    app.include_router(router)
    app.state.router = MultiRouter(providers, enable_failover)
    if browser is not None:
        app.state.browser = browser
    return TestClient(app, raise_server_exceptions=False)


def post(client, **overrides):
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return client.post("/v1/chat/completions", json=body)


# -- failover ------------------------------------------------------------


def test_falls_back_to_next_provider():
    primary = FailingProvider("primary", ProviderError("selectors broke"))
    backup = OkProvider("backup", ["from backup"])
    resp = post(make_client([primary, backup]))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "from backup"
    assert primary.calls == 2  # original + one fresh-tab retry
    assert backup.calls == 1


def test_fallback_uses_its_own_default_model():
    primary = FailingProvider("primary", ProviderError("broke"))
    backup = OkProvider("backup", ["ok"], models=("backup-default",))
    post(make_client([primary, backup]))
    assert backup.last_model == "backup-default"


def test_timeout_skips_same_provider_retry():
    """A second attempt would cost another full timeout, so go straight on."""
    primary = FailingProvider("primary", ProviderTimeout("slow"))
    backup = OkProvider("backup", ["ok"])
    assert post(make_client([primary, backup])).status_code == 200
    assert primary.calls == 1


def test_logged_out_skips_same_provider_retry():
    primary = FailingProvider("primary", AuthenticationRequired("logged out"))
    backup = OkProvider("backup", ["ok"])
    assert post(make_client([primary, backup])).status_code == 200
    assert primary.calls == 1


def test_no_failover_when_provider_is_pinned():
    primary = FailingProvider("primary", ProviderError("broke"))
    backup = OkProvider("backup", ["ok"])
    resp = post(make_client([primary, backup]), model="primary/m")
    assert resp.status_code == 502
    assert backup.calls == 0


def test_no_failover_after_partial_output():
    """Switching mid-answer would splice two different replies together."""
    primary = FailingProvider("primary", ProviderError("died"), deltas=["half "])
    backup = OkProvider("backup", ["whole"])
    resp = post(make_client([primary, backup]), stream=True)
    assert "half " in resp.text
    assert "whole" not in resp.text
    assert backup.calls == 0


def test_failover_disabled_by_config():
    primary = FailingProvider("primary", ProviderError("broke"))
    backup = OkProvider("backup", ["ok"])
    resp = post(make_client([primary, backup], enable_failover=False))
    assert resp.status_code == 502
    assert primary.calls == 1
    assert backup.calls == 0


def test_last_error_surfaces_when_all_providers_fail():
    primary = FailingProvider("primary", ProviderError("broke"))
    backup = FailingProvider("backup", ProviderTimeout("also broke"))
    resp = post(make_client([primary, backup]))
    assert resp.status_code == 504
    assert resp.json()["error"]["type"] == "timeout"


def test_failover_skips_providers_lacking_tool_support():
    primary = FailingProvider("primary", ProviderError("broke"))
    primary.supports_tools = True
    backup = OkProvider("backup", ["ok"])
    backup.supports_tools = False
    tools = [
        {
            "type": "function",
            "function": {"name": "f", "parameters": {"type": "object"}},
        }
    ]
    resp = post(make_client([primary, backup]), tools=tools)
    assert resp.status_code == 502  # backup was not eligible
    assert backup.calls == 0


def test_streaming_failover_emits_only_backup_content():
    primary = FailingProvider("primary", ProviderError("broke"))
    backup = OkProvider("backup", ["hello"])
    resp = post(make_client([primary, backup]), stream=True)
    assert "hello" in resp.text
    assert "data: [DONE]" in resp.text


# -- health --------------------------------------------------------------


class FakeBrowser:
    def __init__(self, is_alive=True, healthy=True):
        self.is_alive = is_alive
        self.healthy = healthy


def test_health_ok_when_browser_alive():
    client = make_client([OkProvider("p", ["x"])], browser=FakeBrowser())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["browser"] == "up"


def test_health_503_when_browser_unrecoverable():
    browser = FakeBrowser(is_alive=False, healthy=False)
    resp = make_client([OkProvider("p", ["x"])], browser=browser).get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unavailable"
    assert resp.json()["browser"] == "down"


def test_health_ok_while_browser_is_merely_down_and_recoverable():
    """A crashed-but-relaunchable browser must not trigger a container restart."""
    browser = FakeBrowser(is_alive=False, healthy=True)
    resp = make_client([OkProvider("p", ["x"])], browser=browser).get("/health")
    assert resp.status_code == 200
    assert resp.json()["browser"] == "down"


# -- browser manager recovery -------------------------------------------


def test_healthy_until_a_relaunch_actually_fails():
    manager = BrowserManager(Settings())
    assert manager.healthy and not manager.is_alive  # cold, not yet started
    manager._start_failed = True
    assert not manager.healthy


def test_context_loss_resets_manager_state():
    manager = BrowserManager(Settings())
    manager._started = True
    manager._context = object()
    manager._pools["p"] = asyncio.Queue()
    manager._pool_pages["p"] = ["page"]
    manager._uses["page"] = 3

    manager._on_context_lost(manager._context)

    assert not manager.is_alive
    assert manager._context is None
    assert not manager._pools and not manager._pool_pages and not manager._uses
    assert manager.healthy  # recoverable: the next acquire() relaunches


def test_expected_close_during_stop_is_ignored():
    manager = BrowserManager(Settings())
    manager._started = True
    manager._closing = True
    manager._pools["p"] = asyncio.Queue()
    manager._on_context_lost(object())
    assert manager._pools  # untouched; stop() owns the teardown


# -- startup policy ------------------------------------------------------


class StubBrowser:
    def __init__(self, start_error=None):
        self._start_error = start_error
        self.is_alive = True
        self.healthy = True
        self.stopped = False

    async def start(self):
        if self._start_error:
            raise self._start_error

    async def stop(self):
        self.stopped = True


def boot(monkeypatch, browser, provider):
    """Run create_app's lifespan with a stubbed browser and default provider."""
    from src.api import app as app_module

    monkeypatch.setattr(app_module, "BrowserManager", lambda config: browser)
    monkeypatch.setattr(
        app_module, "ProviderRouter", lambda config, br: MultiRouter([provider])
    )
    return TestClient(app_module.create_app(Settings(watchdog_interval_s=0)))


def test_unlaunchable_browser_fails_startup(monkeypatch):
    """No browser means every request would 500 forever — exit and let Docker retry."""
    browser = StubBrowser(start_error=RuntimeError("no X server"))
    with pytest.raises(RuntimeError, match="no X server"):
        with boot(monkeypatch, browser, OkProvider("p", ["x"])):
            pass


def test_logged_out_provider_still_serves(monkeypatch):
    """Logging in happens *through* the running container, so it must stay up."""

    class LoggedOut(OkProvider):
        async def startup(self):
            raise AuthenticationRequired("logged out")

    provider = LoggedOut("p", ["x"])
    with boot(monkeypatch, StubBrowser(), provider) as client:
        assert client.get("/health").status_code == 200


@pytest.mark.asyncio
async def test_acquire_times_out_instead_of_hanging():
    """A pool with no free tab must 503, not stall the request forever."""
    manager = BrowserManager(Settings(pool_wait_s=0.05))
    manager._started = True
    manager._context = object()
    manager._pools["p"] = asyncio.Queue()  # empty: every tab is busy

    with pytest.raises(ProviderUnavailable):
        async with manager.acquire("p"):
            pass
