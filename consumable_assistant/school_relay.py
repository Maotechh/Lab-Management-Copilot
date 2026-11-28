"""Private streaming adapter for the pinned DSH Responses provider."""
from __future__ import annotations

import os
import secrets
from contextlib import AsyncExitStack

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import settings

router = APIRouter()


def stream_client():
    return httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(300, connect=5))


class SchoolStream(Response):
    def __init__(self, payload: dict, url: str | None = None):
        super().__init__()
        self.payload = payload
        self.url = url or settings.resolve_response_gateway() or settings.SCHOOL_RESPONSE_URL

    async def __call__(self, scope, receive, send):
        stack = AsyncExitStack()
        try:
            async with anyio.create_task_group() as tasks:
                async def disconnect():
                    while (await receive())["type"] != "http.disconnect":
                        pass
                    tasks.cancel_scope.cancel()

                tasks.start_soon(disconnect)
                try:
                    client = await stack.enter_async_context(stream_client())
                    upstream = await stack.enter_async_context(client.stream(
                        "POST", self.url, json=self.payload,
                        headers={"Authorization": f"Bearer {settings.GENAI_API_KEY}", "Accept": "text/event-stream"},
                    ))
                    if upstream.status_code != 200 or upstream.headers.get("content-type", "").split(";")[0].strip().lower() != "text/event-stream":
                        raise ValueError("Unexpected school gateway response")
                except (httpx.HTTPError, ValueError):
                    await JSONResponse({"detail": "School streaming gateway unavailable"}, 502)(scope, receive, send)
                else:
                    async def chunks():
                        try:
                            async for chunk in upstream.aiter_bytes():
                                yield chunk
                        except httpx.HTTPError:
                            yield b'\n\nevent: error\ndata: {"type":"error","code":"upstream_disconnected","message":"School stream interrupted"}\n\n'

                    stream = StreamingResponse(chunks(), media_type="text/event-stream",
                                               headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
                    await stream.stream_response(send)
                finally:
                    tasks.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                await stack.aclose()


@router.post("/api/internal/school/responses", include_in_schema=False)
async def relay(request: Request):
    expected = os.environ.get("LAB_DSH_RELAY_TOKEN", "")
    authorization = request.headers.get("authorization", "").encode()
    if (not request.client or request.client.host not in {"127.0.0.1", "::1"} or not expected
            or not secrets.compare_digest(authorization, f"Bearer {expected}".encode())):
        raise HTTPException(403, "Local DSH authentication required")
    upstream_url = settings.resolve_response_gateway()
    if not settings.GENAI_API_KEY or not upstream_url:
        raise HTTPException(503, "School gateway not configured")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Invalid Responses request") from None
    if (not isinstance(body, dict) or body.get("model") not in settings.QUALIFIED_MODELS or body.get("stream") is not True
            or not isinstance(body.get("input"), (str, list))):
        raise HTTPException(400, "Expected a streaming qualified Responses request")
    payload = {key: body[key] for key in ("model", "input", "instructions", "stream", "tools", "tool_choice") if key in body}
    payload["model"] = settings.GENAI_MODEL
    inputs = payload["input"]
    if isinstance(inputs, list) and inputs and isinstance(inputs[0], dict) and inputs[0].get("role") in {"system", "developer"}:
        system = inputs[0].get("content")
        if isinstance(system, list) and all(isinstance(part, dict) and part.get("type") == "input_text" and isinstance(part.get("text"), str) for part in system):
            system = "\n".join(part["text"] for part in system)
        if isinstance(system, str) and not payload.get("instructions"):
            payload.update(instructions=system, input=inputs[1:])
    if not isinstance(payload.get("tools", []), list) or any(
        not isinstance(tool, dict) or tool.get("type") != "function" for tool in payload.get("tools", [])
    ):
        raise HTTPException(400, "Only application function tools are supported")
    payload.update(max_output_tokens=settings.GENAI_MAX_OUTPUT_TOKENS, temperature=settings.GENAI_TEMPERATURE)
    return SchoolStream(payload, url=upstream_url)
