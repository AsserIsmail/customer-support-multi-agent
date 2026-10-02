# Customer Support Multi-Agent AI

A customer support assistant combining SQLite customer records with company policy
PDFs. Built incrementally for a technical hiring assessment.

## Implementation status

**Phases 1 through 5: locally runnable support application.** Configuration, reproducible fictional
data, restricted read-only lookups, PDF extraction, OpenAI embeddings, persistent
Chroma search, an official SDK MCP server/client, a LangGraph assistant, and automated
tests are implemented. FastAPI serves chat and PDF uploads; Streamlit provides the
web interface. A terminal chat is also available. Final packaging and Docker are
planned for Phase 6.

## Planned architecture

```text
Streamlit -> FastAPI -> LangGraph supervisor
                        | SQL specialist -> MCP client --+
                        | RAG specialist -> MCP client --+-> MCP server
                        |                                    | SQLite lookups
                        |                                    | Chroma policy search
                        +-> evidence synthesis -> cited answer

PDF upload -> PyMuPDF -> page-aware chunks -> OpenAI embeddings -> Chroma
```

Agents use MCP tools exclusively for customer and policy retrieval. The SQL
specialist calls constrained lookup tools rather than executing generated SQL.
The supervisor routes to either specialist or both, with conversation state
supporting follow-up questions. The graph and data services are independently testable.

## Local setup (PowerShell)

Python 3.10 or newer is required; development was tested on Windows with Python
3.13.3. Use an isolated virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m support_ai.seed
.\.venv\Scripts\python.exe -m pytest -q
```

`requirements-lock.txt` records the tested Windows/Python 3.13 dependency versions.
For the same environment, install it before the editable package:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

On macOS/Linux, use `.venv/bin/python` instead of the Windows interpreter path.
Commands assume the repository root is the current directory.

### Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `SUPPORT_DB_PATH` | `data/support.db` | SQLite path, relative to the working directory |
| `SUPPORT_LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR, or CRITICAL |
| `OPENAI_API_KEY` | Unset | Required for live policy ingestion and nonempty-index search |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding model |
| `OPENAI_CHAT_MODEL` | `gpt-4.1-mini` | Model for routing, specialists, and synthesis |
| `AGENT_TIMEOUT_SECONDS` | `60` | Timeout per model request, 5..180 seconds; one retry |
| `API_HOST` | `127.0.0.1` | API bind address |
| `API_PORT` | `8000` | API port |
| `SUPPORT_API_URL` | `http://127.0.0.1:8000` | Backend URL used by Streamlit |
| `API_CHAT_TIMEOUT_SECONDS` | `300` | Overall chat request deadline, 1..900 seconds |
| `API_SESSION_TTL_SECONDS` | `3600` | Idle conversation lifetime, 1..86400 seconds |
| `API_MAX_SESSIONS` | `100` | Maximum live conversations, 1..1000 |
| `CHROMA_PATH` | `data/chroma` | Local persistent vector store |
| `CHROMA_COLLECTION` | `company_policies` | Index name; change for a fresh policy set |
| `POLICY_CHUNK_SIZE` | `1200` | Maximum characters per chunk (100..4000) |
| `POLICY_CHUNK_OVERLAP` | `200` | Overlapping characters, smaller than chunk size |
| `POLICY_MAX_DISTANCE` | `0.6` | Maximum cosine distance accepted by search (0..2) |

Configuration loads `.env` from the current working directory. Process environment
variables take precedence. Copy `.env.example` to `.env` if it does not already
exist, then edit the copy locally. Keep the API key out of chat, source code, and
Git. The seed command and automated tests need no key. To choose a different database:

```powershell
$env:SUPPORT_DB_PATH = "data/demo.db"
.\.venv\Scripts\python.exe -m support_ai.seed
# Alternatively, supply an explicit path:
.\.venv\Scripts\python.exe -m support_ai.seed --db data/another-demo.db
```

## Run the web application

From the repository root, seed the demo database and generate its fictional PDF:

```powershell
.\.venv\Scripts\python.exe -m support_ai.seed
.\.venv\Scripts\python.exe -m support_ai.demo_pdfs
```

Ensure your local `.env` contains `OPENAI_API_KEY`. Start two terminals:

**Terminal 1 - API**

```powershell
.\.venv\Scripts\python.exe -m support_ai.api
```

**Terminal 2 - Streamlit**

```powershell
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

Open [the support app](http://127.0.0.1:8501). Select the generated
`output/pdf/demo_policies.pdf` in the sidebar and click **Add policy**. If it was
already ingested, the app reports that it is indexed. Then try:

1. "Show Emma's open support tickets."
2. "I mean Emma Wilson."
3. "What was her latest order?"
4. "Can she return that order under the refund policy?"
5. "What is the standard shipping policy?"

**New conversation** clears the current backend conversation and the displayed
messages. Policy documents remain shared across conversations. Each browser
connection keeps its own conversation credentials in Streamlit session state;
the UI never asks for or displays your OpenAI key. Refreshing the browser may
start a new session. Use Ctrl+C in each terminal to stop the servers.

### API endpoints

Interactive endpoint documentation is at [API docs](http://127.0.0.1:8000/docs).

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness and configuration-presence checks; no paid model request |
| `POST /sessions` | Create a random session ID and bearer token |
| `POST /chat` | JSON with `session_id` and `message`; returns answer, sources, status, route, trace |
| `POST /policies?session_id=...` | Multipart `file` PDF upload |
| `DELETE /sessions/{session_id}` | Clear the session and its graph checkpoints |

Chat, upload, and delete require `Authorization: Bearer <session token>`. The token
comes from `/sessions`, not OpenAI. Tokens are returned only when creating a
session, never in chat responses. Another session's token cannot read or clear
your conversation. These temporary conversation credentials do **not** authenticate
a real user or restrict access to particular customer records.

Chat outcomes (`answered`, `needs_clarification`, `insufficient_evidence`, `error`)
are represented in the JSON result. Request failures use HTTP codes: 400 for
invalid PDFs or rejected ingestion, 401 for expired/invalid conversation
credentials, 409 for a busy conversation or upload, 413 for oversized bodies,
422 for invalid inputs, 503 for missing chat setup or session capacity, 504 for
chat timeouts, and 502 for unexpected service failures. Error responses avoid
echoing request contents or raw provider exceptions.

### Local service behavior

- Use **one API worker**. It owns one MCP subprocess, in-memory conversation state,
  and a lock serializing policy ingestion. Multiple workers would split session
  state and would not share the upload lock.
- Idle sessions expire after one hour by default. Expired checkpoints are removed
  lazily on the next session operation. Busy sessions are not expired mid-request.
  An API restart clears all sessions; the UI then prompts for a new conversation.
- PDF files are processed through the existing ingestion service. Original upload
  files are not retained; extracted passages and embeddings persist in Chroma.
  Filenames are metadata, never destination paths. Upload limits are enforced in
  Streamlit and the API, including actual bytes for chunked requests.
- The API starts without an OpenAI key so `/health` and setup diagnostics remain
  available. Chat and new embeddings still require a valid key and API credits.
  A healthy endpoint does not prove provider access or document retrieval quality.
- Both services bind to loopback by default. This is a local synthetic-data demo;
  account authentication, per-customer authorization, durable sessions, and
  deployment hardening are not included. Do not expose the ports publicly.
- API access logging is disabled by the launcher to avoid logging conversation
  identifiers from upload URLs. Operational logs contain failure classes and
  routing decisions, not keys or full request bodies.

## Data and lookups

The seed command creates 25 fictional customers, 75 orders, and 75 tickets using
fixed dates and `example.com` email addresses. Emma Wilson and Emma Chen each have
three historical orders and associated tickets. Amounts are integer cents in USD.
Historical dates are intentional: this fixture does not imply current eligibility
under any future refund policy.

Seeding uses one transaction for all records. An existing database containing any
customer, order, or ticket records is preserved and seeding is skipped. To generate
a fresh demo, choose a new database path. There is no destructive reset option.

Example from Python after installation and seeding:

```python
from support_ai.config import Settings
from support_ai.database import CustomerRepository

records = CustomerRepository(Settings.from_env().db_path)
matches = records.find_customers("Emma")  # Both Emmas; ask which customer.
emma = records.get_customer(1)            # Emma Wilson
orders = records.get_orders(1)
tickets = records.get_tickets(1, limit=10, offset=0)
```

Lookup behavior:

- Name/email search is a literal substring search; all matches are returned.
  SQLite's default case-insensitive matching covers the ASCII demo names.
- Customer IDs must be positive integers. Histories use newest-first ordering
  and pagination, with a maximum of 100 records per request.
- Missing customers return `None`; unmatched searches and histories return `[]`.
  Call `get_customer` to distinguish a missing customer from an empty history.
- Unavailable or malformed databases raise `DataAccessError`, not empty results.
- Lookup connections use SQLite `mode=ro` plus `query_only`. Inputs are bound
  parameters; callers cannot supply SQL through the public lookup methods.
- Foreign keys prevent orphaned records and tickets linked to another customer's
  order. Customer records and search terms are not written to application logs.

The repository is the internal data layer used by the MCP server, not a network
authorization boundary. Authentication and access control must be considered
before exposing real customer records outside a local demo.

## Project layout

```text
src/support_ai/config.py     Environment configuration and logging
src/support_ai/database.py   Schema, connection handling, restricted lookups
src/support_ai/seed.py       Reproducible seed CLI
src/support_ai/policies.py   PDF chunks, OpenAI embeddings, Chroma ingestion/search CLI
src/support_ai/demo_pdfs.py  Reproducible fictional policy PDF generator
src/support_ai/mcp_server.py Restricted MCP tools over stdio
src/support_ai/mcp_client.py MCP-only client and diagnostic CLI
src/support_ai/agent_models.py Typed model decisions and role prompts
src/support_ai/agents.py     LangGraph supervisor, specialists, and conversation state
src/support_ai/chat.py       Terminal chat and scripted conversation CLI
src/support_ai/api.py        FastAPI endpoints, sessions, and input limits
src/support_ai/web_runtime.py Lifespan-owned MCP/agent resources and ingestion
src/support_ai/web_client.py Streamlit HTTP client
streamlit_app.py             Web chat and policy uploads
.streamlit/config.toml       Local binding, theme, and upload limit
tests/test_database.py      Data integrity and lookup tests
tests/test_policies.py      PDF, embedding adapter, persistence, and search tests
tests/test_mcp.py           Real subprocess MCP integration tests
tests/test_agents.py        Graph routing, evidence, follow-ups, and failure tests
tests/test_api.py           HTTP contracts, sessions, uploads, and full retrieval path
tests/test_frontend.py      Streamlit AppTest chat and error tests
.env.example               Configuration names; no credentials
```

## Policy ingestion and search demo

Generate the fictional three-page policy PDF, set your key in `.env`, then run:

```powershell
.\.venv\Scripts\python.exe -m support_ai.demo_pdfs
.\.venv\Scripts\python.exe -m support_ai.policies ingest output/pdf/demo_policies.pdf
.\.venv\Scripts\python.exe -m support_ai.policies search "How long do I have to request a refund?"
.\.venv\Scripts\python.exe -m support_ai.policies search "When should a delayed shipment be investigated?"
```

Ingestion and search send document chunks or query text to OpenAI's embedding API
and incur API usage. PDF creation and extraction are local. Search returns JSON
passages with text, filename, one-based page number, chunk/document IDs, embedding
model, cosine distance, and a citation such as `[demo_policies.pdf, p. 1]`.
The ingestion/search CLI returns evidence passages; the chat application uses
LangGraph to synthesize answers from those passages.

### Ingestion decisions and limits

- PyMuPDF extracts and normalizes text separately for each page. Chunks overlap
  within a page and never cross a page boundary, so citations remain precise.
- PDFs are limited to 20 MiB, 200 pages, 100,000 extracted characters per page,
  and 1,000 chunks. Malformed, locked, empty, scanned, or partly textless PDFs
  are rejected with a clear message. OCR is not included. Complex multi-column
  layouts may need preprocessing; extracted reading order is not guaranteed.
- SHA-256 of the PDF bytes identifies the document. Repeat uploads, including
  renamed copies, return `duplicate` and preserve the original source filename
  without another embedding request. Visually identical PDFs with different bytes
  are different documents.
- A changed document with an existing filename is rejected. To revise a policy
  set, choose a new collection and ingest the intended set there. Do not mix
  obsolete and current versions by renaming them into the same collection.
- Model and chunk settings are stored in collection metadata. Changes require a
  new collection to prevent mixing incompatible embeddings or chunk layouts.
- All embeddings are obtained before the single bounded Chroma upsert. Stable
  chunk IDs make retries idempotent. A provider failure leaves existing records
  unchanged. This is a local, single-writer demo; concurrent uploads/replacements
  are not supported yet. Chroma write failures are reported and may need a retry.
- Search uses cosine distance and a configurable cutoff. Empty results mean no
  evidence passed the cutoff, not proof that the company has no relevant policy.
  The default cutoff is a starting value, not a calibrated confidence score.
- SDK retries are limited to two, with a 30-second request timeout. Operational
  logs report failure classes rather than provider error bodies or document text.

Implementation references: [OpenAI embeddings](https://developers.openai.com/api/docs/guides/embeddings),
[Chroma persistent client](https://docs.trychroma.com/reference/python/client), and
[PyMuPDF text extraction](https://pymupdf.readthedocs.io/en/latest/recipes-text.html).

## MCP server and client

The official `mcp` Python SDK (tested with 2.2.0) runs a local stdio server. The
client launches it as a child process, initializes a protocol session, and calls
its tools. stdout is reserved for MCP messages; operational logs go to stderr.
No network port is opened. Run from the repository root after seeding:

```powershell
# Discover tools and look up Emma through MCP; no API request required.
.\.venv\Scripts\python.exe -m support_ai.mcp_client

# Also search the previously ingested policy collection; uses OpenAI embeddings.
.\.venv\Scripts\python.exe -m support_ai.mcp_client --policy-query "What is the refund window?"
```

To attach an external MCP host, configure the server command as the absolute path
to `.venv/Scripts/python.exe`, arguments as `["-m", "support_ai.mcp_server"]`, and
the working directory as the repository root. The equivalent installed command is
`support-mcp`. Running the server directly waits for protocol input; it is not an
interactive chat terminal.

| Tool | Arguments | Result |
| --- | --- | --- |
| `customer_lookup` | `query` | Matching profiles; `found`, `ambiguous`, or `not_found` |
| `customer_profile` | `customer_id` | One profile or `not_found` |
| `order_lookup` | `customer_id`, optional `limit`, `offset` | Orders with `next_offset` |
| `support_tickets` | `customer_id`, optional `limit`, `offset` | Tickets with `next_offset` |
| `policy_search` | `query`, optional `limit` | Cited passages or `no_evidence` |

Histories distinguish `customer_not_found` from an `empty` page. Tool failures set
the MCP error flag; they are never returned as empty successful searches. Numeric
arguments require actual integers; IDs are positive, history limits are 1..100,
and policy limits are 1..20. Name/email search is literal, not a natural-language
SQL engine. Interpreting full user questions is Phase 4's responsibility.

The tools expose no arbitrary SQL, file reading, upload, ingestion, or mutation
operation. Customer reads retain SQLite's read-only enforcement. Policy tools
open an existing collection; a missing index produces an actionable error rather
than silently creating one. Chroma may maintain internal storage files while
opening/searching its local database, but the tool never adds or modifies policy
records. PDF ingestion remains a separate operator command.

MCP read-only annotations describe behavior; enforcement comes from the restricted
implementations. The local process can access synthetic records for every customer;
this is not per-user authorization. Do not expose it as a public service without
adding authentication and customer access controls.

The agents use this client boundary; it imports no database or policy implementation:

```python
from support_ai.mcp_client import connect_support_tools

async def lookup():
    async with connect_support_tools() as tools:
        return await tools.call("customer_lookup", {"query": "Emma"})
```

Keep connection entry and exit in the same async task. A connection reuses its
server process for multiple calls and closes it on exit. The default request
timeout is 120 seconds. Missing data or credentials fail only the relevant tool;
SQL tools do not need a policy index or OpenAI key.

SDK reference: [official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/).

## Conversational assistant (Phase 4)

After seeding records, ingesting the policy PDF, and setting `OPENAI_API_KEY`, start:

```powershell
.\.venv\Scripts\python.exe -m support_ai.chat
```

Type `/new` to clear the current conversation or `/quit` to exit. For a repeatable
conversation, use multiple `--question` flags in the same invocation:

```powershell
.\.venv\Scripts\python.exe -m support_ai.chat --question "Show Emma's open tickets." --question "I mean Emma Wilson." --question "What was her latest order?" --question "Can she return that order under the policy?"
```

Each scripted response includes `status`, `answer`, `sources`, `route`, and `trace`.
The trace lists the graph nodes that ran, making routing reviewable. The terminal
interface prints the answer. This sends questions and relevant synthetic records
and policy passages to OpenAI for inference. A SQL turn normally makes three model
calls; a combined turn makes four, plus a query-embedding request.

### Graph behavior

```text
START -> supervisor -> SQL specialist -> synthesis -> END
                    -> RAG specialist -> synthesis -> END
                    -> SQL specialist -> RAG specialist -> synthesis -> END
```

Clarification or a retrieval failure ends the turn early. Each path also runs a
small finish node to record conversation history.

- **Supervisor:** uses a structured model decision to choose SQL, RAG, both,
  clarification, or an out-of-scope reply. It rewrites follow-ups into standalone
  requests and separates a new topic from an unfinished prior request.
- **SQL specialist:** plans a customer lookup and the required histories. Python
  executes only the named MCP lookup tools. Multiple customer matches produce a
  deterministic clarification listing actual matches. A new customer replaces the
  previously selected customer. Optional order/ticket status filters are applied
  to retrieved records.
- **RAG specialist:** produces a focused policy query and calls `policy_search`
  through MCP. No matching policy evidence prevents a policy conclusion.
- **Synthesis:** receives fresh evidence from this turn, then returns typed answer
  blocks with evidence IDs. Unknown IDs or missing references are rejected. Policy
  citations are rendered by Python from retrieved metadata, rather than invented
  by the model. Combined answers must reference both SQL and policy evidence.
- **Conversation state:** LangGraph's in-memory checkpointer separates threads.
  The prompt sees at most six recent turns plus the selected customer and pending
  request. `/new` deletes that thread's checkpoints. State is lost on process exit;
  durable history and user authentication are not implemented. Requests for the
  same thread cannot run concurrently.

### Limits and error handling

The graph has no autonomous tool loop. SQL retrieval is bounded to four pages of
50 records per requested history, and up to 50 matching records are sent to
synthesis. Evidence marks incomplete retrieval and omitted records. Policy search
returns at most five passages. Questions are capped at 4,000 characters. Model
requests have a configurable timeout and one retry. Errors are returned as an
explicit `error` status without raw provider details or stale answers.

Prompts treat documents and tickets as untrusted data and require clarification
for missing facts. In particular, order dates do not establish delivery dates, so
the demo cannot confirm return eligibility merely from a shipped order.

Citation validation verifies reference provenance; it does not prove every
generated claim is supported. Routing and synthesis remain model-driven and can
make mistakes. Use the recorded trace and returned sources to assess answers.
If an answer fails evidence validation, the application returns an error instead
of displaying the unverified draft. This is a local assessment demo, not an
automated refund or customer account management system.

References: [LangGraph graph API](https://docs.langchain.com/oss/python/langgraph/graph-api),
[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence),
[LangChain OpenAI integration](https://docs.langchain.com/oss/python/integrations/chat/openai), and
[OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

## Validation and demo questions

Tests cover deterministic seeds, preservation of existing data, Emma ambiguity,
missing information, literal search inputs, ID/pagination validation, read-only
enforcement, foreign keys, and configuration. Tests create isolated temporary
databases and do not require network access or an OpenAI key. Policy tests use
real PDF extraction and Chroma storage with deterministic test vectors. They
exercise page citations, duplicate handling, persistence across processes,
provider failures, index compatibility, and upload limits. Mocked OpenAI adapter
tests verify batching and error handling, not live semantic quality. Run the
ingest/search commands above for a live check after configuring the key.

MCP tests perform discovery and tool calls over actual stdio subprocess connections.
They check ambiguity, pagination, invalid arguments, rejected undeclared tools,
unchanged customer database contents, missing data stores, error isolation, and
policy citations with real Chroma storage. The test policy server injects fixed
vectors; production exposes no fake-embedding mode. A separate live MCP search
checks the OpenAI integration using the configured local key.

Agent tests use the real compiled LangGraph with scripted model decisions. They
cover routing, fresh evidence per turn, ambiguity, follow-ups, thread isolation,
history clearing, bounded pagination, status filtering, source validation, and
model/tool failures. One test runs the graph against a real MCP subprocess with
seeded SQLite records. These deterministic tests verify orchestration; live model
checks separately assess interpretation and generated answers.

Example questions for the terminal or web chat:

- "Show Emma's support tickets." (Should clarify which Emma.)
- "I mean Emma Wilson. What was her latest order?"
- "What does the refund policy say?" (Requires uploaded policy PDFs.)
- "Can Emma Wilson return her latest order under the policy?" (Requires both
  customer history and cited policy evidence; clarify missing facts.)

Each phase is reviewed separately. Do not commit credentials, `.env`, uploaded
documents, generated databases, Chroma storage, logs, or virtual environments.
These local artifacts are covered by `.gitignore`.
