import pytest
from fastapi.testclient import TestClient

from src.providers.base import BaseChatProvider

from .conftest import FakeProvider, make_app


class DynamicProvider(BaseChatProvider):
    """A provider whose model catalogue is discovered from the 'website'."""

    name = "dyn"
    default_model = "seed"
    available_models = ("seed",)

    async def list_models(self):
        return ["m-a", "m-b", "m-a"]  # includes a duplicate on purpose

    async def generate(self, request):
        yield "ok"


async def test_refresh_populates_and_dedupes_catalogue():
    provider = DynamicProvider(None, None)
    assert provider.models == ["seed"]  # seed before discovery
    await provider.refresh_models()
    assert provider.models == ["m-a", "m-b"]  # discovered + deduped
    assert provider.supports_model("m-a")
    assert not provider.supports_model("seed")


async def test_models_endpoint_reflects_live_catalogue():
    provider = DynamicProvider(None, None)
    await provider.refresh_models()
    client = TestClient(make_app(provider))
    data = client.get("/v1/models").json()
    assert [m["id"] for m in data["data"]] == ["m-a", "m-b"]
    assert all(m["owned_by"] == "dyn" for m in data["data"])


async def test_discovery_failure_keeps_defaults():
    class Broken(DynamicProvider):
        async def list_models(self):
            raise RuntimeError("site unreachable")

    provider = Broken(None, None)
    await provider.refresh_models()
    assert provider.models == ["seed"]  # fell back, no crash


async def test_unknown_model_is_rejected():
    provider = DynamicProvider(None, None)
    await provider.refresh_models()  # catalogue: m-a, m-b
    client = TestClient(make_app(provider))

    bad = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert bad.status_code == 404

    ok = client.post(
        "/v1/chat/completions",
        json={"model": "m-a", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert ok.status_code == 200


async def test_omitted_model_uses_provider_default():
    provider = DynamicProvider(None, None)
    await provider.refresh_models()
    client = TestClient(make_app(provider))
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},  # no model
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == provider.default_model
