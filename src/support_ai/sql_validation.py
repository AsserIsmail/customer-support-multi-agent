"""Fail-closed Oracle SELECT subset. Only regenerated, validated SQL is executed."""

from dataclasses import dataclass
import logging
import math
import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.optimizer.qualify import qualify

from support_ai.config import OracleSettings

# Parser diagnostics may echo SQL. The caller emits only safe application errors.
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)


class QueryError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str
    parameters: dict
    tables: tuple[str, ...]


# Exact classes: adding a new parser node does not implicitly grant it access.
ALLOWED_NODES = {
    exp.Select, exp.From, exp.Table, exp.TableAlias, exp.Identifier, exp.Column, exp.Star,
    exp.Alias, exp.Join, exp.Where, exp.Group, exp.Having, exp.Order, exp.Ordered,
    exp.Subquery, exp.With, exp.CTE, exp.Distinct, exp.Literal, exp.Null, exp.Boolean,
    exp.Placeholder, exp.Paren, exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.GT,
    exp.GTE, exp.LT, exp.LTE, exp.Is, exp.In, exp.Between, exp.Like, exp.Escape,
    exp.Exists, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.DPipe,
    exp.Count, exp.Sum, exp.Min, exp.Max, exp.Avg, exp.Lower, exp.Upper, exp.Length,
    exp.Abs, exp.Round, exp.Coalesce, exp.Case, exp.If, exp.DateStrToDate,
    exp.Limit, exp.Offset, exp.Fetch, exp.LimitOptions,
}
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_$#]{0,127}")
BIND_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


def validate_query(sql: str, parameters: dict | None, settings: OracleSettings) -> ValidatedQuery:
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 12000:
        raise QueryError("invalid_sql", "SQL must contain 1..12000 characters")
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict) or len(parameters) > 50:
        raise QueryError("invalid_parameters", "Supply at most 50 named scalar parameters")
    for name, value in parameters.items():
        if not isinstance(name, str) or not BIND_NAME.fullmatch(name):
            raise QueryError("invalid_parameters", "Parameter names must be lowercase simple identifiers")
        if (type(value) not in (str, int, float, type(None))
                or isinstance(value, str) and len(value) > 4000
                or type(value) is int and not -(2**63) <= value < 2**63
                or type(value) is float and not math.isfinite(value)):
            raise QueryError("invalid_parameters", "Parameter values must be bounded text, finite numbers, or null")
    try:
        statements = sqlglot.parse(sql, read="oracle", error_level=sqlglot.ErrorLevel.RAISE)
        if len(statements) != 1 or type(statements[0]) is not exp.Select:
            raise QueryError("invalid_sql", "Exactly one SELECT statement is required")
        tree = normalize_identifiers(statements[0], dialect="oracle")
        nodes = list(tree.walk())
        if len(nodes) > 500:
            raise QueryError("invalid_sql", "SQL exceeds the supported complexity limit")
        for node in nodes:
            if type(node) not in ALLOWED_NODES:
                raise QueryError("invalid_sql", "SQL contains an unsupported or unsafe construct")
            if isinstance(node, exp.Identifier) and not IDENTIFIER.fullmatch(node.name):
                raise QueryError("invalid_sql", "SQL identifiers must use simple names")
            if isinstance(node, exp.Column) and (node.catalog or node.db or node.name.upper() in {
                "NEXTVAL", "CURRVAL", "USER", "UID", "CURRENT_USER", "SESSION_USER", "SYSTEM_USER",
                "ROWID", "ROWNUM", "ORA_ROWSCN", "LEVEL",
            }):
                raise QueryError("invalid_sql", "Object attributes, sequences, and pseudocolumns are not allowed")
            if isinstance(node, exp.With) and node.args.get("recursive"):
                raise QueryError("invalid_sql", "Recursive queries are not supported")
            if isinstance(node, exp.Join) and (node.args.get("method") or node.args.get("kind") in {"CROSS", "SEMI", "ANTI"}
                                             or not (node.args.get("on") or node.args.get("using"))):
                raise QueryError("invalid_sql", "Joins must specify ON or USING")
            if isinstance(node, exp.LimitOptions) and (node.args.get("percent") or node.args.get("with_ties")):
                raise QueryError("invalid_sql", "Percentage and WITH TIES limits are not supported")
            if isinstance(node, exp.Placeholder):
                name = str(node.this).lower()
                if not BIND_NAME.fullmatch(name):
                    raise QueryError("invalid_parameters", "Only named scalar bind variables are supported")
                node.set("this", name)
        names = {node.this for node in tree.find_all(exp.Placeholder)}
        if names != set(parameters):
            raise QueryError("invalid_parameters", "Parameters must exactly match the SQL bind names")
        seen, physical = set(), {}
        for scope in traverse_scope(tree):
            for _, (node, source) in scope.selected_sources.items():
                if isinstance(node, exp.Table):
                    seen.add(id(node))
                if isinstance(source, exp.Table):
                    if (source.catalog or not isinstance(source.this, exp.Identifier)
                            or source.name not in settings.allowed_tables
                            or source.db and source.db != settings.schema.upper()):
                        raise QueryError("unauthorized_object", "Query references an unauthorized database object")
                    physical[id(source)] = source
                elif not isinstance(source, Scope):
                    raise QueryError("invalid_sql", "Unsupported query source")
        if {id(table) for table in tree.find_all(exp.Table)} != seen or not physical or len(physical) > 8:
            raise QueryError("invalid_sql", "Query must use between one and eight approved table references")
        tables = tuple(sorted({table.name for table in physical.values()}))
        for table in physical.values():
            table.set("this", exp.to_identifier(table.name, quoted=True))
            table.set("db", exp.to_identifier(settings.schema.upper(), quoted=True))
        # Strip comments and regenerate: unsupported original text is never sent to Oracle.
        rendered = tree.sql(dialect="oracle", comments=False, unsupported_level=sqlglot.ErrorLevel.RAISE)
        return ValidatedQuery(rendered, dict(parameters), tables)
    except QueryError:
        raise
    except Exception:
        raise QueryError("invalid_sql", "SQL could not be parsed or validated as a supported Oracle SELECT") from None


def qualify_query(query: ValidatedQuery, owner: str, columns: dict[str, dict[str, str]]) -> str:
    """Resolve every column against fresh metadata, including CTE and alias scopes.

    This also prevents a dotted identifier from being interpreted by Oracle as a
    no-argument package function instead of a real column.
    """
    if set(columns) != set(query.tables) or any(not values for values in columns.values()):
        raise QueryError("unauthorized_object", "Approved table columns are unavailable to the runtime account")
    try:
        tree = qualify(sqlglot.parse_one(query.sql, read="oracle"), dialect="oracle", schema={owner: columns})
        if len(tree.selects) > 50:
            raise QueryError("unsupported_result", "Select at most 50 result columns")
        return tree.sql(dialect="oracle", comments=False, unsupported_level=sqlglot.ErrorLevel.RAISE)
    except QueryError:
        raise
    except Exception:
        raise QueryError("invalid_columns", "Query columns or aliases do not match the accessible schema") from None
