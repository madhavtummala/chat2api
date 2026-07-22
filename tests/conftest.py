import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import router
from src.core.errors import ProviderError
from src.core.types import ChatRequest
from src.providers.base import BaseChatProvider


class FakeProvider(BaseChatProvider):
    name = "fake"
    default_model = "fake-1"
    available_models = ("fake-1", "fake-2")
    supports_tools = True

    def __init__(self, deltas=None, error: ProviderError | None = None):
        # Bypass BaseChatProvider.__init__ (no settings/browser needed here).
        self._deltas = deltas if deltas is not None else ["Hello", " ", "world"]
        self._error = error

    def supports_model(self, model: str) -> bool:
        return True  # test double accepts any model id

    async def generate(self, request: ChatRequest):
        for delta in self._deltas:
            yield delta
        if self._error:
            raise self._error


class FakeRouter:
    """Single-provider stand-in for ProviderRouter used by the API tests.

    Implements just the surface the routes touch: model splitting, lookup, and
    enumeration. A bare or ``<name>/``-prefixed model both resolve to the one
    wrapped provider; any other provider prefix is unknown (KeyError -> 404).
    """

    def __init__(self, provider: BaseChatProvider):
        self._provider = provider

    @property
    def default_name(self) -> str:
        return self._provider.name

    @property
    def enabled(self) -> list[str]:
        return [self._provider.name]

    def get(self, name: str) -> BaseChatProvider:
        if name == self._provider.name:
            return self._provider
        raise KeyError(name)

    def all_providers(self) -> list[BaseChatProvider]:
        return [self._provider]

    def split(self, model: str) -> tuple[str, str]:
        prefix, sep, rest = model.partition("/")
        if sep and prefix == self._provider.name:
            return prefix, rest
        return self._provider.name, model


def make_app(provider: BaseChatProvider) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.router = FakeRouter(provider)
    return app


@pytest.fixture
def client():
    return TestClient(make_app(FakeProvider()))
