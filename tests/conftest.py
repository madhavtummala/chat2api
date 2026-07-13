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


def make_app(provider: BaseChatProvider) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.provider = provider
    return app


@pytest.fixture
def client():
    return TestClient(make_app(FakeProvider()))
