import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from assessment.gateway import Settings, create_app


@asynccontextmanager
async def gateway(tmp_path, handler, **kwargs):
    settings = Settings(
        tokens={"viewer-secret": "viewer", "admin-secret": "admin"},
        database=str(tmp_path / "usage.db"),
        **kwargs,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        app = create_app(settings, upstream)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer viewer-secret"},
            ) as client:
                yield client


async def test_mcp_auth_and_forwarding(tmp_path):
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {"tools": []}})

    async with gateway(tmp_path, handler) as client:
        message = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "admin_reset_key"},
        }
        denied = await client.post("/mcp", json=message)
        assert denied.json()["error"]["code"] == -32001
        assert not seen
        await client.post("/mcp", json=message, headers={"Authorization": "Bearer admin-secret"})
        assert len(seen) == 1
        assert "authorization" not in seen[0].headers
        await client.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
        assert len(seen) == 2
        assert (await client.post("/mcp", content="{")).json()["error"]["code"] == -32700
        assert (await client.post("/mcp", json=[])).json()["error"]["code"] == -32600
        assert (
            await client.post("/mcp", json=message, headers={"Authorization": "Bearer admin"})
        ).status_code == 401


@pytest.mark.parametrize("mode", ["ok", "429", "timeout", "500", "both-fail"])
async def test_routing(tmp_path, mode):
    paths = []
    cancelled = []

    async def handler(request):
        paths.append(request.url.path)
        if "backup" not in request.url.path:
            if mode == "timeout":
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise
            if mode in {"429", "both-fail"}:
                return httpx.Response(429)
            if mode == "500":
                return httpx.Response(500, text="SECRET STACK TRACE")
        if mode == "both-fail":
            return httpx.Response(503, text="SECRET STACK TRACE")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 12}}
        )

    async with gateway(tmp_path, handler, primary_timeout=0.01) as client:
        response = await client.post("/v1/completions", json={"prompt": "hello"})
        assert len(paths) == (2 if mode in {"429", "timeout", "both-fail"} else 1)
        assert "SECRET" not in response.text
        if mode in {"500", "both-fail"}:
            assert response.status_code == 502
        else:
            assert response.json()["fallback"] == (mode != "ok")
        if mode == "timeout":
            assert cancelled


async def test_rate_limit_before_upstream(tmp_path):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(500)

    async with gateway(tmp_path, handler) as client:
        response = await client.post("/v1/completions", json={"prompt": "😀" * 9000})
        assert response.status_code == 429
        assert not calls


async def test_stream(tmp_path):
    from assessment.mock import app as mock

    settings = Settings(tokens={"token": "viewer"}, database=str(tmp_path / "usage.db"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock)) as upstream:
        app = create_app(settings, upstream)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                result = await client.post(
                    "/v1/stream",
                    json={"prompt": "hello"},
                    headers={"Authorization": "Bearer token"},
                )
                assert result.status_code == 200
                assert "alice" not in result.text
                assert "123-45" not in result.text
                assert "4111" not in result.text
                assert result.text.count("[REDACTED]") == 3
                assert "[DONE]" in result.text


async def test_sse_delivers_without_waiting_for_end():
    from assessment.gateway import sse_data

    gate = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"first": true}\r\n\r\n'
            await gate.wait()
            yield b"data: [DONE]\n\n"

    response = httpx.Response(200, stream=SlowStream())
    iterator = sse_data(response)
    assert await asyncio.wait_for(anext(iterator), 0.1) == '{"first": true}'
    gate.set()
    assert await anext(iterator) == "[DONE]"
    await response.aclose()


async def test_truncated_stream_does_not_flush_candidate(tmp_path):
    async def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"alice@exam"}}]}\n\n',
        )

    async with gateway(tmp_path, handler) as client:
        response = await client.post("/v1/stream", json={"prompt": "hello"})
        assert "alice" not in response.text
        assert "stream_failed" in response.text
        assert "[DONE]" not in response.text
