import asyncio
import json
import os
import sys

import pytest
from mcp import types
from mcp.shared.exceptions import McpError

from assessment.mcp_server import call_tool


@pytest.mark.parametrize(
    "arguments",
    [
        {"customer_id": "CUST-1234"},
        {"customer_id": "CUST-１２３４５"},
        {"customer_id": 12345},
        {"customer_id": "CUST-12345", "extra": True},
    ],
)
async def test_bad_customer(arguments):
    with pytest.raises(McpError) as exc:
        await call_tool(
            types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(name="get_customer_record", arguments=arguments),
            )
        )
    assert exc.value.error.code == -32602


@pytest.mark.parametrize("amount", [0, -1, "5", True, float("inf"), float("nan")])
async def test_bad_refund(amount):
    with pytest.raises(McpError):
        await call_tool(
            types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(
                    name="trigger_refund",
                    arguments={
                        "customer_id": "CUST-12345",
                        "amount": amount,
                        "reason": "Duplicate payment",
                    },
                ),
            )
        )


async def test_stdio_wire():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "assessment.mcp_server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )

    async def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()

    async def read():
        line = await asyncio.wait_for(process.stdout.readline(), 5)
        result = json.loads(line)
        assert result["jsonrpc"] == "2.0"
        return result

    try:
        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        assert "result" in await read()
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert len((await read())["result"]["tools"]) == 2
        for request_id, args in [(3, {"customer_id": "bad"}), (4, {"customer_id": "CUST-12345"})]:
            await send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": "get_customer_record", "arguments": args},
                }
            )
            result = await read()
            assert result["id"] == request_id
            if request_id == 3:
                assert result["error"]["code"] == -32602
            else:
                assert result["result"]["structuredContent"]["customer_id"] == "CUST-12345"
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 5)
        assert await process.stdout.read() == b""
        assert b"Processing request" in await process.stderr.read()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
