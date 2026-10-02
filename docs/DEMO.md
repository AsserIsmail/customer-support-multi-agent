# Hiring assessment demonstration

## Before presenting

Follow the README setup. Confirm the API is running and upload the generated
fictional `demo_policies.pdf`. Never display `.env`, session tokens, or provider
billing details on screen. The browser is sufficient for the main demo.

## Five-minute walkthrough

| Action | Expected behavior | What it demonstrates |
| --- | --- | --- |
| Ask "Show Emma's open support tickets." | Lists Emma Wilson and Emma Chen and asks which one | Ambiguity resolved using actual records |
| Reply "I mean Emma Wilson." | Open ticket 103, Delivery tracking | Conversation state and status filtering |
| Ask "What was her latest order?" | Order 103, Monitor, $299.00, shipped | Follow-up resolution and SQLite retrieval |
| Ask "Can she return that order under the refund policy?" | Explains the 30-day policy with page 1 citation and asks for the missing delivery date | SQL plus RAG, citations, and missing information |
| Ask "What is the standard shipping policy?" | Standard shipping 3 to 7 business days after dispatch; page 2 citation | Topic change and policy-only routing |
| Upload the same PDF again | Reports that it is already indexed | Duplicate detection without additional document embeddings |
| Select New conversation and ask "What was her order?" | Asks which customer | Conversation isolation |

Answers are model-generated, so exact wording varies. Do not present a shipped
order as delivered or promise refund eligibility based on the order date.

## Technical walkthrough

1. Show the architecture diagram in README.
2. Open `mcp_server.py`: five explicit tools, no arbitrary SQL or write tool.
3. Open `agents.py`: supervisor, SQL, RAG, synthesis, and the graph's conditional edges.
4. Explain that the SQL specialist plans lookup calls, not generated SQL execution.
5. Show `tests/test_mcp.py` and `tests/test_api.py`: actual MCP subprocess tests and
   an HTTP-to-graph-to-MCP-to-SQLite integration test.
6. Run `python -m pytest -q` with the virtualenv interpreter.
7. For a visible route trace, run:

```powershell
.\.venv\Scripts\python.exe -m support_ai.chat --question "Can Emma Wilson return her latest order under the refund policy?"
```

The JSON result should show route `both`, executed nodes, answer status, and
source metadata. Credentials are never part of this output.

## Explain the limits honestly

- This is a local synthetic-data demo. Session tokens isolate conversation state;
  they are not real customer authentication or record-level authorization.
- Conversation checkpoints are in memory and disappear on restart.
- Scanned PDFs need OCR before ingestion. Revised policies use a new collection.
- Source validation checks provenance, not the truth of every generated claim.
- Offline tests use scripted model decisions and deterministic embeddings. Live
  checks exercise OpenAI separately and cost API credits.
- Refunds and record mutations are outside the tool surface.
