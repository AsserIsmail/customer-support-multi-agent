from support_ai.evaluate import checks_for


def result(route="sql", status="answered", sources=None):
    return {"route": route, "status": status, "sources": sources or []}


def test_oracle_combined_clarification_still_requires_execution():
    checks = checks_for(result("both", "needs_clarification", ["policy"]), [], "both",
                        {"needs_clarification"}, "oracle", "combined")
    assert not checks["oracle_executed"]


def test_customer_ambiguity_does_not_require_execution():
    checks = checks_for(result(status="needs_clarification"), [], "sql",
                        {"needs_clarification"}, "oracle", "ambiguity")
    assert all(checks.values())


def test_late_discovery_does_not_hide_earlier_ungrounded_query():
    calls = [{"name": name} for name in ["execute_readonly_query", "list_tables", "search_schema",
        "describe_table", "get_relationships", "execute_readonly_query"]]
    assert not checks_for(result(), calls, "sql", {"answered"}, "oracle")["discovery_before_execution"]


def test_policy_answer_requires_citations_and_correct_route():
    checks = checks_for(result("sql"), [], "rag", {"answered"}, "sqlite")
    assert not checks["policy_citations"] and not checks["expected_route"]


def test_complete_discovery_passes_structural_check():
    calls = [{"name": name} for name in ["list_tables", "search_schema", "describe_table",
        "get_relationships", "execute_readonly_query"]]
    assert all(checks_for(result(), calls, "sql", {"answered"}, "oracle").values())
