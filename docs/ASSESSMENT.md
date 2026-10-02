# Implementation map

| Assessment requirement | Implementation | Verification |
| --- | --- | --- |
| Python, FastAPI, Streamlit | `api.py`, `streamlit_app.py`, `web_client.py` | API tests, Streamlit AppTest, live browser check |
| LangGraph supervisor and specialists | `agents.py`, `agent_models.py` | Scripted graph tests and live conversations |
| LangChain integration | `ChatOpenAI.with_structured_output` | Live structured routing and synthesis |
| OpenAI inference and embeddings | `agent_models.py`, `policies.py` | Separate live inference/embedding checks |
| SQLite customer records | `database.py`, `seed.py` | Deterministic seed and read-only lookup tests |
| 20-30 customers, Emma history | 25 customers, including two Emmas; 75 orders and 75 tickets | Database fixture tests |
| PyMuPDF ingestion, Chroma persistence | `policies.py` | Extraction, invalid PDF, persistence, and duplicate tests |
| Official MCP server, agents use tools | `mcp_server.py`, `mcp_client.py`; graph imports only client boundary | Real SDK stdio discovery and call tests |
| Natural-language customer/ticket queries | Supervisor and SQL planner plus bounded MCP lookup execution | Live Emma/ticket/order conversation |
| Combined SQL and policy answers | `both` route and source-checked synthesis | Graph tests and live return-eligibility question |
| Conversation and follow-ups | LangGraph checkpoints and API session registry | Follow-up, reset, expiration, and isolation tests |
| PDF uploads and citations | `/policies`, sidebar upload, metadata-based citation rendering | API pipeline with Chroma and browser source display |
| Missing/ambiguous data | Explicit states; actual customer match clarification | Missing customer/index and ambiguity tests |
| Read-only, parameterized SQL | SQLite `mode=ro`, fixed query templates, bound values | Injection, write rejection, foreign-key tests |
| Configuration and logs | `.env.example`, `config.py`, restricted operational logging | Configuration precedence and error redaction tests |
| Reproducible scripts and docs | Seed/PDF CLIs, lockfile, README, demo guide | Repeated seed/PDF tests and package build |
| Docker if practical | Dockerfile, Compose, restricted build context | See validation record for tested versus unverified checks |

## Scope decisions

The SQL specialist does not receive arbitrary SQL execution. The MCP server exposes
five narrow retrieval tools. This satisfies natural-language retrieval while making
read-only enforcement independent of model behavior.

Specialists use structured model plans and application-controlled MCP calls. There
is no unbounded autonomous tool loop. The supervisor chooses the execution path;
the synthesis node combines the evidence and supplies references for validation.

Policy uploads are an explicit operator action through the API, outside the agents'
read-only tool surface. No generated private artifacts or API credentials belong in
Git or the Docker image.
