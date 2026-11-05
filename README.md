# AI gateway assessment

Four Python implementations: an official-SDK MCP server, an authenticated MCP proxy, an SSE redaction gateway, and a SQLite-backed completion router.

## Run locally

Requires Python 3.12 or newer; tested with Python 3.12. No provider account is needed for the mock demo.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
pip install --no-deps -e .
cp .env.example .env
```

On Windows PowerShell, use `.venv\Scripts\Activate.ps1` and `Copy-Item .env.example .env` instead of the activation and copy commands above.

Start the mock in terminal 1:

```bash
uvicorn assessment.mock:app --host 127.0.0.1 --port 9001
```

Start the gateway in terminal 2, with the virtual environment activated:

```bash
uvicorn assessment.gateway:app --env-file .env --host 127.0.0.1 --port 8000
```

Interactive HTTP docs: http://127.0.0.1:8000/docs. Enter a bearer header through curl examples below; the docs do not include an authentication widget.

## Task 1 — stdio MCP server

```bash
customer-mcp
```

Use an MCP client to connect to that executable, or configure it to run `.venv/bin/python -m assessment.mcp_server` (Windows: `.venv\Scripts\python.exe`). The process waits for MCP messages on stdin; it is not an HTTP server.

| Tool | Arguments |
| --- | --- |
| `get_customer_record` | `customer_id`: exactly `CUST-` and five ASCII digits |
| `trigger_refund` | Same customer ID; finite numeric `amount > 0`; string `reason` of at least ten characters |

Pydantic strict mode rejects numeric strings, booleans as amounts, extra fields, NaN and infinity. JSON numbers such as `10` and `10.5` are accepted as amounts. Invalid arguments and unknown tools return JSON-RPC `-32602`. The pinned SDK's normal tool decorator wraps exceptions as tool results, so this server registers its typed call handler directly to preserve the required protocol error code. The SDK owns initialization and transport framing. All logging goes to stderr.

Customer records are synthetic. Refunds return `status: simulated`; no payment service is called. The subprocess test initializes a real server and checks success, errors, and stdout purity.

## Task 2 — MCP authorization proxy

```bash
curl http://127.0.0.1:8000/mcp \
  -H 'Authorization: Bearer local-viewer-token' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

curl http://127.0.0.1:8000/mcp \
  -H 'Authorization: Bearer local-viewer-token' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
```

The second call returns `-32001: Unauthorized Tool Call` without contacting the mock. Change the token to `local-admin-token` to allow it.

Tokens are opaque secrets resolved against a trusted server-side role map. A client cannot obtain admin access by sending `Bearer admin` or an unsigned JWT. `tools/list` is forwarded unchanged, including admin tool descriptions, as requested. Actual execution is authorized separately. Gateway credentials are not forwarded downstream.

This is the assessment's HTTP/JSON-RPC proxy for a mock downstream, not a complete Streamable HTTP MCP transport: no downstream SSE session management or JSON-RPC batch support. Notifications receive no JSON-RPC reply; blocked notification calls are dropped with HTTP 202. Malformed JSON uses `-32700`, invalid envelopes `-32600`, invalid params `-32602`, and downstream failures a sanitized `-32002`.

## Task 3 — streaming PII guardrail

```bash
curl -N http://127.0.0.1:8000/v1/stream \
  -H 'Authorization: Bearer local-viewer-token' \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Show the mock response"}'
```

The mock deliberately splits an email, SSN and card number across deltas. The client receives SSE `data: {"delta":"..."}` events with all three replaced by `[REDACTED]`, followed by `[DONE]`. This normalized output format differs from OpenAI's response envelope.

The scanner holds an unfinished ASCII word until a boundary, then scans digit runs across spaces and hyphens. That avoids releasing the beginning of a sensitive value before the next chunk arrives. Ordinary completed words are emitted immediately. Candidate storage is bounded to 320 characters for words and 128 for numeric runs; oversized candidates are conservatively replaced and discarded through their boundary. SSE events and lines are capped at 64 KiB. The full response is never accumulated.

Tradeoff: exact cross-chunk detection requires delaying an unfinished candidate. A provider that pauses in the middle of an email can delay that candidate; a fixed timer that flushes it would risk disclosure. Regex coverage is conventional ASCII emails, dashed US SSNs, and 13–19 digit card-like sequences with spaces or hyphens. This intentionally redacts card-like values without a Luhn check. It is not a universal detector for Unicode/obfuscated PII, quoted email addresses, or every national identifier.

Provider metadata, tool-call arguments and extra choices are not passed through as text. On a truncated/failed stream, an error SSE event is emitted and the unfinished candidate is discarded. Cancellation closes the upstream stream. HTTP status cannot change after streaming has started.

## Task 4 — token budget and fallback

```bash
curl http://127.0.0.1:8000/v1/completions \
  -H 'Authorization: Bearer local-viewer-token' \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"force-429","max_tokens":128}'
```

Use `force-timeout` to demonstrate the 3000 ms primary deadline, or any other prompt for primary success. The response identifies the selected model, whether fallback occurred, and charged tokens. Primary 429 or timeout triggers one backup call. Other HTTP errors do not trigger fallback. The backup has a ten-second deadline. `asyncio.timeout` covers the entire primary attempt and cancels it before the backup starts.

SQLite uses WAL, an index, and `BEGIN IMMEDIATE` to make admission checks plus reservations atomic across concurrent workers sharing the same on-disk database. API keys are stored only as SHA-256 tenant hashes. Settled usage expires exactly 60 seconds after settlement; pending calls continue counting until settled.

Each request reserves both possible attempts up front. Per attempt, the reservation is UTF-8 prompt bytes + 32 framing tokens + requested maximum output. This is a conservative estimate for byte-level tokenizers, not a claim of exact model-specific tokenization. Trusted reported total usage replaces the successful attempt's estimate; absent usage keeps the estimate. A timed-out/failed primary keeps its full reservation because its actual usage is unknown. Errors and cancellation retain conservative charges. Reserving both attempts can reject a request that a primary-only strategy could accept; it prevents fallback from bypassing the tenant budget.

A crashed worker's pending reservation is converted to a full charge after 120 seconds at the next admission check, then retained for another 60 seconds. This bounds stale reservations without granting fresh capacity immediately after a crash. The lease is longer than the bounded 13-second provider sequence.

Task 3 and Task 4 are separate assessment endpoints. The streaming endpoint does not use this limiter or fallback, and the non-streaming endpoint does not redact PII. A production combined gateway would also account for streamed usage and must never switch providers after emitting response text.

## Configuration and deployment notes

Set `PRIMARY_URL` and `SECONDARY_URL` to complete OpenAI-compatible chat completion endpoint URLs; configure separate API keys and model names. Provider requests use `messages`, `max_tokens`, and `stream`. Providers requiring different token-limit fields or response formats need an adapter.

Keep `.env` out of Git. Replace local demo credentials, use HTTPS at the ingress, restrict access to downstream services, and persist the SQLite file on a local disk volume. WAL is for workers on one machine, not a database shared over a network filesystem. Add ingress body-size limits and connection limits for an internet-facing deployment. Refunds need payment integration and idempotency before real use. Mock services bind to localhost by default.

For a zero-trust deployment failure, check DNS and egress to configured provider URLs, TLS certificates, the service's own upstream credentials, ingress buffering, and access to the database directory. Do not forward the user's gateway bearer token to a provider.

## Tests

```bash
pytest -q
ruff check .
ruff format --check .
```

Tests cover stdio JSON-RPC, strict schemas, denied calls never reaching downstream, every two-part split of supported PII patterns, single-character chunks, bounded candidate memory, SSE delivery before EOF, truncated streams, concurrent SQLite admission, persistent windows, crash recovery, primary cancellation, fallback, and sanitized errors.

`requirements.lock` records the exact environment used for verification. GitHub Actions runs tests and lint on push and pull requests.

## Repository

https://github.com/hemasaieswar/Ai-Field-Developer-Test

```bash
git clone https://github.com/hemasaieswar/Ai-Field-Developer-Test.git
cd Ai-Field-Developer-Test
```

Follow the setup instructions above after cloning.

## Files

- `src/assessment/mcp_server.py`: Task 1
- `src/assessment/gateway.py`: Task 2, Task 3 HTTP/SSE handling, Task 4 routing
- `src/assessment/redaction.py`: Task 3 incremental scanner
- `src/assessment/limiter.py`: Task 4 SQLite admission and accounting
- `src/assessment/mock.py`: local downstream MCP and provider demos
- `tests/`: protocol, security, streaming and concurrency checks
