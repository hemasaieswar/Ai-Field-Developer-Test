import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

app = FastAPI()


@app.post("/mcp")
async def mcp(request: Request):
    body = await request.json()
    if "id" not in body:
        return Response(status_code=202)
    if body["method"] == "tools/list":
        result = {
            "tools": [
                {"name": name, "inputSchema": {"type": "object"}}
                for name in ["get_customer_record", "admin_reset_key"]
            ]
        }
    else:
        result = {"content": [{"type": "text", "text": "Mock tool executed"}]}
    return {"jsonrpc": "2.0", "id": body["id"], "result": result}


@app.post("/v1/chat/completions")
@app.post("/backup/v1/chat/completions")
async def completion(request: Request):
    body = await request.json()
    prompt = body["messages"][0]["content"]
    primary = not request.url.path.startswith("/backup")
    if primary and prompt == "force-429":
        return JSONResponse({"error": "Mock quota"}, status_code=429)
    if primary and prompt == "force-timeout":
        await asyncio.sleep(4)
    if body.get("stream"):

        async def chunks():
            for part in [
                "Contact ",
                "alice@",
                "example.com",
                "; SSN ",
                "123-",
                "45-6789",
                "; card ",
                "4111 1111 ",
                "1111 1111",
                ". Done.",
            ]:
                yield (
                    "data: "
                    + json.dumps({"choices": [{"index": 0, "delta": {"content": part}}]})
                    + "\n\n"
                )
                await asyncio.sleep(0.03)
            yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")
    return {
        "choices": [{"message": {"content": "Completed by " + body["model"]}}],
        "usage": {"total_tokens": 24},
    }
