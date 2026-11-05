import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from assessment.limiter import RateLimited, TokenLimiter
from assessment.redaction import Redactor


@dataclass
class Settings:
    tokens: dict[str, str] = field(
        default_factory=lambda: json.loads(os.environ.get("GATEWAY_TOKENS", "{}"))
    )
    downstream: str = field(
        default_factory=lambda: os.getenv("MCP_URL", "http://127.0.0.1:9001/mcp")
    )
    primary: str = field(
        default_factory=lambda: os.getenv(
            "PRIMARY_URL", "http://127.0.0.1:9001/v1/chat/completions"
        )
    )
    secondary: str = field(
        default_factory=lambda: os.getenv(
            "SECONDARY_URL", "http://127.0.0.1:9001/backup/v1/chat/completions"
        )
    )
    primary_key: str = field(default_factory=lambda: os.getenv("PRIMARY_API_KEY", ""))
    secondary_key: str = field(default_factory=lambda: os.getenv("SECONDARY_API_KEY", ""))
    primary_model: str = field(default_factory=lambda: os.getenv("PRIMARY_MODEL", "demo-primary"))
    secondary_model: str = field(
        default_factory=lambda: os.getenv("SECONDARY_MODEL", "demo-backup")
    )
    database: str = field(default_factory=lambda: os.getenv("DATABASE_PATH", "usage.sqlite3"))
    primary_timeout: float = 3.0
    secondary_timeout: float = 10.0


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=10_000)
    max_tokens: int = Field(default=256, ge=1, le=4096)


def error(code: str, message: str, status: int):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def rpc_error(request_id, code, message, status=200):
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


def identity(request: Request, settings: Settings):
    parts = request.headers.get("authorization", "").split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    role = settings.tokens.get(parts[1])
    return (parts[1], role) if role in {"viewer", "admin"} else None


def payload(body: Completion, model: str, stream=False):
    return {
        "model": model,
        "messages": [{"role": "user", "content": body.prompt}],
        "max_tokens": body.max_tokens,
        "stream": stream,
    }


def provider_headers(key: str):
    return {"Authorization": f"Bearer {key}"} if key else {}


async def sse_data(response: httpx.Response):
    buffer = b""
    data = []
    size = 0
    async for chunk in response.aiter_bytes():
        buffer += chunk
        if len(buffer) > 65_536:
            raise ValueError("Oversized SSE line")
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                if data:
                    yield b"\n".join(data).decode("utf-8")
                data, size = [], 0
            elif line.startswith(b"data:"):
                part = line[5:].removeprefix(b" ")
                size += len(part)
                if size > 65_536:
                    raise ValueError("Oversized SSE event")
                data.append(part)
    if buffer or data:
        raise ValueError("Truncated SSE event")


def event(value):
    return "data: " + json.dumps(value) + "\n\n"


def create_app(settings: Settings | None = None, client: httpx.AsyncClient | None = None):
    settings = settings or Settings()
    limiter = TokenLimiter(settings.database)

    @asynccontextmanager
    async def lifespan(app):
        await limiter.initialize()
        app.state.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        try:
            yield
        finally:
            if client is None:
                await app.state.client.aclose()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return error("invalid_request", "Invalid request fields", 422)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        return error("internal_error", "Gateway could not complete the request", 500)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/mcp")
    async def mcp_proxy(request: Request):
        who = identity(request, settings)
        if who is None:
            return rpc_error(None, -32001, "Authentication required", 401)
        try:
            raw = await request.body()
            if len(raw) > 65_536:
                return rpc_error(None, -32600, "Request too large", 413)
            message = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return rpc_error(None, -32700, "Parse error")
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or "id" in message
            and (
                isinstance(message["id"], bool)
                or not isinstance(message["id"], (str, int, type(None)))
            )
        ):
            return rpc_error(None, -32600, "Invalid Request")
        request_id = message.get("id")
        notification = "id" not in message
        params = message.get("params", {})
        if not isinstance(params, dict):
            return (
                Response(status_code=202)
                if notification
                else rpc_error(request_id, -32602, "Invalid params")
            )
        if message["method"] == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return (
                    Response(status_code=202)
                    if notification
                    else rpc_error(request_id, -32602, "Invalid params")
                )
            if name.startswith("admin_") and who[1] != "admin":
                return (
                    Response(status_code=202)
                    if notification
                    else rpc_error(request_id, -32001, "Unauthorized Tool Call")
                )
        try:
            response = await app.state.client.post(
                settings.downstream, content=raw, headers={"Content-Type": "application/json"}
            )
            if response.is_error:
                return rpc_error(request_id, -32002, "Downstream unavailable", 502)
            if notification:
                return Response(status_code=202)
            return Response(
                response.content,
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "application/json"),
            )
        except httpx.HTTPError:
            return rpc_error(request_id, -32002, "Downstream unavailable", 502)

    @app.post("/v1/stream")
    async def stream(body: Completion, request: Request):
        if identity(request, settings) is None:
            return error("unauthorized", "Valid bearer token required", 401)
        upstream = app.state.client.build_request(
            "POST",
            settings.primary,
            json=payload(body, settings.primary_model, True),
            headers=provider_headers(settings.primary_key),
        )
        try:
            response = await app.state.client.send(upstream, stream=True)
        except httpx.HTTPError:
            return error("upstream_unavailable", "Provider unavailable", 502)
        if response.is_error or "text/event-stream" not in response.headers.get("content-type", ""):
            await response.aclose()
            return error("upstream_unavailable", "Provider unavailable", 502)

        async def generate():
            redactor = Redactor()
            try:
                async for data in sse_data(response):
                    if data == "[DONE]":
                        tail = redactor.finish()
                        if tail:
                            yield event({"delta": tail})
                        yield "data: [DONE]\n\n"
                        return
                    parsed = json.loads(data)
                    for choice in parsed.get("choices", []):
                        if choice.get("index", 0) != 0:
                            raise ValueError("Multiple choices unsupported")
                        delta = choice.get("delta", {}).get("content")
                        if delta is not None:
                            if not isinstance(delta, str):
                                raise ValueError("Non-text delta")
                            safe = redactor.feed(delta)
                            if safe:
                                yield event({"delta": safe})
                raise ValueError("Missing completion marker")
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                # An incomplete candidate is discarded when the provider fails.
                yield event(
                    {"error": {"code": "stream_failed", "message": "Provider stream interrupted"}}
                )
            finally:
                await response.aclose()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/completions")
    async def complete(body: Completion, request: Request):
        who = identity(request, settings)
        if who is None:
            return error("unauthorized", "Valid bearer token required", 401)
        # UTF-8 bytes + framing is a conservative estimate for byte-level tokenizers.
        budget = len(body.prompt.encode("utf-8")) + 32 + body.max_tokens
        try:
            reservation = await limiter.reserve(who[0], 2 * budget)
        except RateLimited:
            return error("rate_limit_exceeded", "Tenant token budget exceeded", 429)
        charge = 2 * budget
        failed_primary = False
        try:
            try:
                async with asyncio.timeout(settings.primary_timeout):
                    response = await app.state.client.post(
                        settings.primary,
                        json=payload(body, settings.primary_model),
                        headers=provider_headers(settings.primary_key),
                    )
                failed_primary = response.status_code == 429
            except (TimeoutError, httpx.TimeoutException):
                failed_primary = True
            if failed_primary:
                async with asyncio.timeout(settings.secondary_timeout):
                    response = await app.state.client.post(
                        settings.secondary,
                        json=payload(body, settings.secondary_model),
                        headers=provider_headers(settings.secondary_key),
                    )
            if response.is_error:
                return error(
                    "upstream_unavailable", "Providers could not complete the request", 502
                )
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("Invalid completion")
            usage = result.get("usage", {}).get("total_tokens")
            actual = usage if type(usage) is int and usage >= 0 else budget
            charge = actual + (budget if failed_primary else 0)
            return {
                "model": settings.secondary_model if failed_primary else settings.primary_model,
                "content": content,
                "usage": {"charged_tokens": charge},
                "fallback": failed_primary,
            }
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, IndexError, TypeError):
            return error("upstream_unavailable", "Providers could not complete the request", 502)
        finally:
            await asyncio.shield(limiter.settle(reservation, charge))

    return app


app = create_app()
