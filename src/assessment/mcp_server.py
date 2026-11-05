import asyncio
import json
import logging
import sys
from typing import Annotated
from uuid import uuid4

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class CustomerInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    customer_id: Annotated[str, Field(pattern=r"^CUST-[0-9]{5}$")]


class RefundInput(CustomerInput):
    amount: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    reason: Annotated[str, Field(min_length=10)]


server = Server("customer-service")
SCHEMAS = {"get_customer_record": CustomerInput, "trigger_refund": RefundInput}


@server.list_tools()
async def list_tools():
    return [
        types.Tool(name=name, inputSchema=model.model_json_schema())
        for name, model in SCHEMAS.items()
    ]


async def call_tool(request: types.CallToolRequest) -> types.ServerResult:
    model = SCHEMAS.get(request.params.name)
    if model is None:
        raise McpError(types.ErrorData(code=-32602, message="Unknown tool"))
    try:
        args = model.model_validate(request.params.arguments or {})
    except ValidationError as exc:
        fields = [{"field": ".".join(map(str, e["loc"])), "type": e["type"]} for e in exc.errors()]
        raise McpError(
            types.ErrorData(code=-32602, message="Invalid params", data=fields)
        ) from None
    if isinstance(args, RefundInput):
        result = {"refund_id": str(uuid4()), "status": "simulated", **args.model_dump()}
    else:
        result = {"customer_id": args.customer_id, "name": "Demo Customer", "status": "active"}
    return types.ServerResult(
        types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result))],
            structuredContent=result,
        )
    )


# The 1.x call_tool decorator converts exceptions into tool results. Register
# directly so invalid arguments reach the wire as JSON-RPC -32602 errors.
server.request_handlers[types.CallToolRequest] = call_tool


async def run():
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
