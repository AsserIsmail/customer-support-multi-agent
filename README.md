# Customer Support Multi-Agent AI

A customer support assistant combining SQLite customer records with company policy
PDFs. Built incrementally for a technical hiring assessment.

## Implementation status

**Phases 1 through 3: database, PDF retrieval, and MCP.** Configuration, reproducible fictional
data, restricted read-only lookups, PDF extraction, OpenAI embeddings, persistent
Chroma search, an official SDK MCP server/client, and automated tests are implemented. LangGraph, FastAPI,
Streamlit, and Docker are planned in later phases; there is no chat/upload UI yet.
The ingestion service accepts PDF bytes for the future upload endpoint.

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

Agents will use MCP tools exclusively for customer and policy retrieval. The SQL
specialist will call constrained lookup tools rather than execute generated SQL.
The supervisor will route to either specialist or both, with conversation state
supporting follow-up questions. The supervisor and agents are not yet implemented;
the MCP boundary and data services are implemented and independently testable.

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

The repository is an internal data layer for the future MCP server, not a network
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
tests/test_database.py      Data integrity and lookup tests
tests/test_policies.py      PDF, embedding adapter, persistence, and search tests
tests/test_mcp.py           Real subprocess MCP integration tests
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
This phase returns evidence passages; answer synthesis arrives with LangGraph.

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

Future agents should use this client boundary; it imports no database or policy
implementation:

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

## Validation and upcoming demo

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

Example questions for the **future** chat interface:

- "Show Emma's support tickets." (Should clarify which Emma.)
- "I mean Emma Wilson. What was her latest order?"
- "What does the refund policy say?" (Requires uploaded policy PDFs.)
- "Can Emma Wilson return her latest order under the policy?" (Requires both
  customer history and cited policy evidence; clarify missing facts.)

Each phase is reviewed separately. Do not commit credentials, `.env`, uploaded
documents, generated databases, Chroma storage, logs, or virtual environments.
These local artifacts are covered by `.gitignore`.
