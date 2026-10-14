# Phase 7 live evaluation ? partial completion

Date: 2026-10-13 (America/Toronto). Model: `gpt-4.1-mini` (configured default).
Backend evaluated: SQLite. Real model calls and policy embeddings; no scripted inference.

| Scenario | Before routing correction | After correction and evidence review |
| --- | --- | --- |
| Ambiguous Emma | Clarified | Correctly listed Emma Wilson and Emma Chen |
| Clarified customer | Answered | Latest order 103, Monitor, $299, shipped |
| Open-ticket follow-up | Answered | Open ticket 103, delivery tracking |
| Status aggregation | Answered | One delivered, one refunded, one shipped |
| Orders alongside tickets | Wrongly routed to policy search; no answer | SQL route; orders/tickets 101, 102, 103 |
| Shipping and refund eligibility | Asked for missing facts with citations | Asked for delivery date/condition; cited policy pages 1 and 2 |
| Switch to general refund policy | Answered | Policy-only answer with page 1 citation |

Fix: clarify the supervisor prompt that `both` means database evidence plus policy
information, not two database entities. Records-only JOINs and aggregates use `sql`.

The first attempted run failed authentication before any model decision. After the
key was corrected locally, the first completed run exposed the routing issue above.
The post-fix run passed seven structural checks, and its answers were compared with
the baseline records and policy passages captured in the local report. No secrets
or raw reports are committed. This small single-run smoke check does not establish
statistical accuracy or Oracle SQL-generation quality.

Outstanding: local Docker daemon fails to start, no local Oracle connection is
configured, Oracle live-model evaluation and UI review have not run, and no merge
has been performed. The earlier automated Oracle CI checks remain separate evidence.

Reproduce with `python -m support_ai.evaluate --backend oracle` after provisioning
Oracle and configuring credentials locally. Use `--backend sqlite` only to evaluate
the compatibility path. Full reports are written under ignored `output/evaluation/`.

Validation: 288 local tests passed (nine Oracle-dependent skips); targeted post-fix
agent/evaluation tests passed. Linux, Windows, container, and automated Oracle CI
jobs all passed: [validation run](https://github.com/AsserIsmail/customer-support-multi-agent/actions/runs/38492142268).
These Oracle CI tests use deterministic inference and do not remove the local live-model blocker.
