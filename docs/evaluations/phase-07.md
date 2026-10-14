# Phase 7 live evaluation

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

Oracle evaluation completed on 2026-10-14 after updating Docker Desktop, removing a
stale Docker extension process, provisioning the reader account, and migrating the
25 customers, 75 orders, and 75 tickets. Credentials remain in ignored local `.env`.

The first Oracle run passed six scenarios but failed the combined question: the
model referenced `RT.order_id` without projecting it from its CTE, then repeated the
same invalid query on repair. Validation blocked both queries. The executor now
returns `invalid_columns` for column/alias resolution failures, and the planner
prompt explicitly checks CTE projections and join keys. The bounded retry remains one.
A regression test covers the missing CTE join key and its corrected projection.

The rerun passed all seven structural checks. Manual review confirmed:

- Latest order 103: Monitor, 29900 cents, USD, shipped, placed 2025-05-11.
- Open support ticket 103 with the stored delivery-tracking description.
- One order per delivered/refunded/shipped status using a generated GROUP BY query.
- Order/ticket pairs 103/103, 102/102, 101/101 with both composite join conditions.
- Combined answer cited policy page 1 and requested missing delivery information.
- General-policy follow-up used policy evidence rather than stale customer records.

Browser verification used an API whose health endpoint reported `oracle`. The web
UI displayed the correct status counts and a refund answer citing `demo_policies.pdf`,
page 1, while asking for delivery information. SQLite UI source expansion and new
conversation reset were also verified during setup.

A local deadline test initially failed because each `localhost` connection took
about 2.07 seconds. Using explicit IPv4 loopback reduced this to about 0.039 seconds;
the local DSN and setup example now use `127.0.0.1`. No timeout protection was weakened.
No final merge has been performed.

Reproduce with `python -m support_ai.evaluate --backend oracle` after provisioning
Oracle and configuring credentials locally. Use `--backend sqlite` only to evaluate
the compatibility path. Full reports are written under ignored `output/evaluation/`.

Validation: 288 local tests passed (nine Oracle-dependent skips); targeted post-fix
agent/evaluation tests passed. Linux, Windows, container, and automated Oracle CI
jobs all passed: [validation run](https://github.com/AsserIsmail/customer-support-multi-agent/actions/runs/38492142268).
These Oracle CI tests use deterministic inference; the live runs described above are separate evidence.
