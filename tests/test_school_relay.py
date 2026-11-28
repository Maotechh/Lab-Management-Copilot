import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from consumable_assistant import school_relay as relay
from scripts import configure_environment as environment


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def app(monkeypatch):
    application = FastAPI()
    application.include_router(relay.router)
    monkeypatch.setenv("LAB_DSH_RELAY_TOKEN", "local-test")
    monkeypatch.setattr(relay.settings, "GENAI_BASE_URL", relay.settings.SCHOOL_RESPONSE_URL)
    monkeypatch.setattr(relay.settings, "GENAI_API_KEY", "upstream-test")
    return application


async def post(app, body=None, token="local-test", host="127.0.0.1"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 7000)), base_url="http://local") as client:
        return await client.post("/api/internal/school/responses", headers={"Authorization": "Bearer " + token},
                                 json=body if body is not None else {"input": "test", "model": "deepseek-pro", "stream": True})


class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts, fail=False):
        self.parts, self.fail, self.closed = parts, fail, False

    async def __aiter__(self):
        for part in self.parts:
            yield part
        if self.fail:
            raise httpx.ReadError("sensitive upstream error")

    async def aclose(self):
        self.closed = True


def mock_upstream(monkeypatch, handler):
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(relay, "stream_client", lambda: upstream)
    return upstream


@pytest.mark.anyio
async def test_relay_rejects_invalid_requests_without_contacting_school(app, monkeypatch):
    monkeypatch.setattr(relay, "stream_client", lambda: pytest.fail("Unexpected upstream request"))
    assert (await post(app, token="")).status_code == 403
    assert (await post(app, token="upstream-test")).status_code == 403
    assert (await post(app, host="10.10.10.10")).status_code == 403
    assert (await post(app, {"input": "test", "model": "other", "stream": True})).status_code == 400
    assert (await post(app, {"input": "test", "model": "deepseek-pro", "stream": False})).status_code == 400
    assert (await post(app, {"input": "test", "model": "deepseek-pro", "stream": True, "tools": [{"type": "web_search"}]})).status_code == 400
    monkeypatch.setattr(relay.settings, "GENAI_BASE_URL", "https://other.invalid/")
    assert (await post(app)).status_code == 503
    monkeypatch.setattr(relay.settings, "GENAI_BASE_URL", relay.settings.SCHOOL_RESPONSE_URL)
    monkeypatch.setattr(relay.settings, "GENAI_API_KEY", "")
    assert (await post(app)).status_code == 503
    monkeypatch.delenv("LAB_DSH_RELAY_TOKEN")
    assert (await post(app)).status_code == 403


@pytest.mark.anyio
async def test_dsh_stream_preserves_function_roundtrip(app, monkeypatch):
    call = {"type": "function_call", "name": "inventory_search", "call_id": "call_1", "arguments": '{"query":"fixture"}'}
    tools = [{"type": "function", "name": "inventory_search", "parameters": {"type": "object"}}]
    inputs = [{"role": "user", "content": "fixture"}]
    for suffix, event in [([], {"type": "response.output_item.done", "item": call}),
                          ([call, {"type": "function_call_output", "call_id": "call_1", "output": "17"}],
                           {"type": "response.output_text.delta", "delta": "17"})]:
        stream = Chunks([b"data: " + json.dumps(event).encode() + b"\n\n", b'data: {"type":"response.completed"}\n\n'])
        expected = {"input": inputs + suffix, "instructions": "Use the fixture tool", "model": "deepseek-pro", "stream": True, "tools": tools}

        def handler(request):
            assert str(request.url) == relay.settings.SCHOOL_RESPONSE_URL
            assert request.headers["Authorization"] == "Bearer upstream-test"
            assert json.loads(request.content) == {
                **expected,
                "model": relay.settings.GENAI_MODEL,
                "max_output_tokens": relay.settings.GENAI_MAX_OUTPUT_TOKENS,
                "temperature": 0.5,
            }
            return httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream; charset=utf-8"})

        upstream = mock_upstream(monkeypatch, handler)
        dsh_body = dict(expected)
        instructions = dsh_body.pop("instructions")
        dsh_body["input"] = [{"role": "system", "content": [{"type": "input_text", "text": instructions}]}, *expected["input"]]
        result = await post(app, {**dsh_body, "max_output_tokens": 65536, "store": True, "url": "https://unused.invalid/"})
        assert result.status_code == 200 and result.content == b"".join(stream.parts)
        assert result.headers["X-Accel-Buffering"] == "no"
        assert stream.closed and upstream.is_closed


@pytest.mark.anyio
@pytest.mark.parametrize("status,media", [(200, "application/json"), (401, "application/json"), (307, "text/event-stream")])
async def test_upstream_errors_are_closed_and_redacted(app, monkeypatch, status, media):
    stream = Chunks([b"sensitive response"])
    upstream = mock_upstream(monkeypatch, lambda _: httpx.Response(status, stream=stream, headers={"Content-Type": media}))
    result = await post(app)
    assert result.status_code == 502 and b"sensitive" not in result.content
    assert stream.closed and upstream.is_closed


@pytest.mark.anyio
async def test_upstream_stream_failure(app, monkeypatch):
    stream = Chunks([b"data: {}\n\n"], fail=True)
    upstream = mock_upstream(monkeypatch, lambda _: httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"}))
    result = await post(app)
    assert b'"type":"error"' in result.content and b"sensitive" not in result.content
    assert stream.closed and upstream.is_closed


@pytest.mark.anyio
async def test_disconnect_closes_pending_upstream(app, monkeypatch):
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.Event().wait()

    upstream = mock_upstream(monkeypatch, handler)

    async def disconnect():
        await started.wait()
        return {"type": "http.disconnect"}

    await asyncio.wait_for(relay.SchoolStream({})({"type": "http"}, disconnect, None), timeout=2)
    assert upstream.is_closed


@pytest.mark.parametrize("endpoint,key,status,expected_path", [
    ("http://retired.invalid", "test", None, None),
    (relay.settings.SCHOOL_RESPONSE_URL, "", None, None),
    (relay.settings.SCHOOL_RESPONSE_URL, "test", 401, "/v1/responses"),
    (relay.settings.SCHOOL_RESPONSE_URL, "test", 200, "/v1/responses"),
    (relay.settings.DEFAULT_BASE_URL, "test", 200, "/v1/responses"),
    (relay.settings.LEGACY_SCHOOL_RESPONSE_URL, "test", 200, "/api/v1/response"),
])
def test_deployment_preflight_is_read_only(tmp_path, monkeypatch, endpoint, key, status, expected_path):
    path = tmp_path / "environment"
    original = f"GENAI_BASE_URL={endpoint}\nGENAI_API_KEY={key}\n"
    path.write_text(original)
    calls = []
    connection = SimpleNamespace(request=lambda *args: calls.append(args), close=lambda: None,
                                 getresponse=lambda: nullcontext(SimpleNamespace(status=status)))
    monkeypatch.setattr(environment.http.client, "HTTPSConnection", lambda *args, **kw: connection)
    with (nullcontext() if status == 200 else pytest.raises(SystemExit)):
        environment.preflight(path)
    assert path.read_text() == original
    if status:
        assert calls[0][:2] == ("POST", expected_path) and calls[0][3]["Authorization"] == "Bearer test"
    else:
        assert not calls
