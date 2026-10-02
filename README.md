# Customer Support Multi-Agent AI

A customer support assistant combining SQLite customer records with company policy
PDFs. Built incrementally for a technical hiring assessment.

## Implementation status

**Phase 1: database foundation.** Configuration, reproducible fictional data,
restricted read-only lookups, and database tests are implemented. MCP, document
ingestion, LangGraph, FastAPI, Streamlit, and Docker are planned in later phases;
there is no chat interface yet.

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
supporting follow-up questions. These components are not yet implemented.

## Local setup (PowerShell)

Python 3.10 or newer is required. Phase 1 has no runtime dependencies outside the
Python standard library. Use an isolated virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m support_ai.seed
.\.venv\Scripts\python.exe -m pytest -q
```

On macOS/Linux, use `.venv/bin/python` instead of the Windows interpreter path.
Commands assume the repository root is the current directory.

### Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `SUPPORT_DB_PATH` | `data/support.db` | SQLite path, relative to the working directory |
| `SUPPORT_LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR, or CRITICAL |
| `OPENAI_API_KEY` | Unset | Reserved for the embedding and inference phases |

Phase 1 reads process environment variables directly; it does **not** automatically
load `.env`. `.env.example` documents the names for future application setup.
No API key is required now. To choose a different database:

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
tests/test_database.py      Data integrity and lookup tests
.env.example               Configuration names; no credentials
```

## Validation and upcoming demo

Tests cover deterministic seeds, preservation of existing data, Emma ambiguity,
missing information, literal search inputs, ID/pagination validation, read-only
enforcement, foreign keys, and configuration. Tests create isolated temporary
databases and do not require network access or an OpenAI key.

Example questions for the **future** chat interface:

- "Show Emma's support tickets." (Should clarify which Emma.)
- "I mean Emma Wilson. What was her latest order?"
- "What does the refund policy say?" (Requires uploaded policy PDFs.)
- "Can Emma Wilson return her latest order under the policy?" (Requires both
  customer history and cited policy evidence; clarify missing facts.)

Each phase is reviewed separately. Do not commit credentials, `.env`, uploaded
documents, generated databases, Chroma storage, logs, or virtual environments.
These local artifacts are covered by `.gitignore`.
