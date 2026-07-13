import base64

from fastapi.testclient import TestClient

from src.api.schemas import ChatCompletionRequest

from .conftest import FakeProvider, make_app


def _req(**kw) -> ChatCompletionRequest:
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    body.update(kw)
    return ChatCompletionRequest(**body)


# ---- web search resolution ----------------------------------------------
def test_web_search_via_online_suffix():
    cr = _req(model="GPT OSS 120B:online").to_chat_request()
    assert cr.web_search is True
    assert cr.model == "GPT OSS 120B"  # suffix stripped for the provider


def test_web_search_via_native_options():
    assert _req(web_search_options={"search_context_size": "high"}).resolve_web_search() is True


def test_web_search_via_vendor_field_wins():
    assert _req(web_search=True).resolve_web_search() is True
    # An explicit false overrides the :online suffix.
    assert _req(web_search=False, model="x:online").resolve_web_search() is False


def test_web_search_default_off():
    assert _req().resolve_web_search() is False


# ---- attachment decoding -------------------------------------------------
def test_file_attachment_decoded_from_content_part():
    data_url = "data:text/plain;base64," + base64.b64encode(b"hello").decode()
    req = ChatCompletionRequest(
        model="m",
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "see file"},
            {"type": "file", "file": {"filename": "a.txt", "file_data": data_url}},
        ]}],
    )
    atts = req.collect_attachments()
    assert len(atts) == 1
    assert atts[0].name == "a.txt" and atts[0].data == b"hello"
    # Text is still extracted for the prompt.
    assert req.to_chat_request().messages[-1].content == "see file"


def test_image_url_data_decoded_and_remote_skipped():
    data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG").decode()
    req = ChatCompletionRequest(model="m", messages=[{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": data_url}},
        {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
    ]}])
    atts = req.collect_attachments()
    assert len(atts) == 1  # remote URL skipped
    assert atts[0].mime == "image/png"


# ---- capability gating ---------------------------------------------------
def test_attachments_rejected_when_provider_unsupported():
    data_url = "data:text/plain;base64," + base64.b64encode(b"hi").decode()
    client = TestClient(make_app(FakeProvider()))  # supports_attachments=False
    resp = client.post("/v1/chat/completions", json={
        "model": "fake-1",
        "messages": [{"role": "user", "content": [
            {"type": "file", "file": {"filename": "a.txt", "file_data": data_url}},
        ]}],
    })
    assert resp.status_code == 400


def test_web_search_soft_ignored_when_unsupported():
    client = TestClient(make_app(FakeProvider()))  # supports_web_search=False
    resp = client.post("/v1/chat/completions", json={
        "model": "fake-1:online",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 200  # ignored, not an error
