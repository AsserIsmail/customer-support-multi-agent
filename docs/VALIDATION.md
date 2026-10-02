# Validation record

Recorded October 2, 2026. This separates completed local checks from configuration
that has not yet executed in its target environment.

## Completed

| Check | Result |
| --- | --- |
| Full automated suite, Windows/Python 3.13.3 | 105 passed in the final delivery run |
| Installed dependency consistency | `pip check`: no broken requirements |
| Database generation | 25 customers, 75 orders, 75 tickets; repeat seed preserves records |
| Demo PDF | Reproducible three-page document; every page visually inspected |
| Live OpenAI embeddings | Three demo policy chunks embedded and persisted |
| Live retrieval | Refund question retrieved page 1; shipping question retrieved page 2 |
| Duplicate ingestion | Same PDF returns `duplicate` without another embedding request |
| Official MCP connection | Real subprocess discovery and calls; live policy search succeeded |
| Live agent conversations | Clarification, customer follow-ups, combined evidence, and topic change exercised |
| Live API | Health, duplicate upload, two-turn Emma conversation, and session deletion succeeded |
| Live browser | Streamlit rendered, accepted a policy question, and displayed its answer and source citation |
| Wheel build | Standard isolated build succeeded; archive contains no `.env` files or databases |
| Fresh installation | Locked dependencies installed into a new Python 3.13 virtualenv; `pip check` passed |
| Installed-wheel smoke | Outside the source checkout: database seed, real MCP customer/order calls, graph import, and API lifespan/health/session creation passed |
| Repository scan | No OpenAI-style key strings, `.env`, or database files found in tracked/unignored source files |
| Compose configuration | `docker compose config --quiet` succeeded without displaying expanded secrets |

The repository scan is a targeted check, not a general secret-detection guarantee.
The local `.env`, databases, vector store, generated PDFs, logs, and virtualenvs
remain ignored.

## Live issues found and corrected

The first embedding attempt was rejected for exhausted provider credits. After
the user fixed billing, live ingestion and retrieval succeeded.

An initial agent conversation carried a pending return question into a new shipping
question. The supervisor prompt was tightened and the topic-change retest used
the RAG-only route. A combined draft omitted its customer-evidence reference and
was withheld; after clarifying the synthesis instructions, the live retest used
both sources and asked for the missing delivery date.

These examples are smoke checks, not a statistical accuracy evaluation. Model
outputs remain variable. Tests validate citation provenance and orchestration;
they cannot establish that every possible answer is factually correct.

## Not verified here

- **Container build and startup:** Docker CLI and Compose are installed. Docker
  Desktop was started, but the engine remained unresponsive to a bounded readiness
  check. No successful image build, container test run, or Compose startup is claimed.
- **Linux execution:** the dependency list has a Windows-only marker for `pywin32`;
  Linux may resolve additional platform dependencies. Linux behavior is pending CI
  or a working Docker engine.
- **Hosted GitHub Actions:** a Windows/Linux test matrix and container test job are
  configured. Hosted CI results have not yet been verified.
- **Production deployment:** no load test, public deployment, customer identity
  provider, per-record authorization, or durable conversation service is included.

## Repeat the checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m build --wheel --outdir dist
docker compose config --quiet
```

For container checks after Docker's Linux engine is ready:

```powershell
docker build --target test -t support-tests .
docker run --rm support-tests
docker compose up --build -d
docker compose ps
```

Live chat and embedding checks require the local API key and sufficient provider
credits. See the demo guide for questions and expected evidence.
