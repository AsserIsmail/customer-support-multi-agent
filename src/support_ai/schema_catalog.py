"""Oracle dictionary discovery and a bounded, process-local metadata index.

Only the configured owner and allowlisted tables are exposed. No customer rows,
embeddings, DBA privileges, DDL, or schema modifications are involved.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from threading import RLock
from time import monotonic
from typing import Callable

import oracledb

from support_ai.config import OracleSettings

logger = logging.getLogger(__name__)
MAX_METADATA_ROWS = 20000


class SchemaError(RuntimeError):
    """Sanitized discovery failure, distinct from an empty search."""


class OracleMetadataLoader:
    def __init__(self, settings: OracleSettings):
        self.settings = settings

    def load(self) -> dict:
        settings = self.settings
        binds = {"owner": settings.schema}
        binds.update({f"t{i}": name for i, name in enumerate(settings.allowed_tables)})
        placeholders = ", ".join(f":t{i}" for i in range(len(settings.allowed_tables)))

        def scope(alias, owner="owner"):
            return f"{alias}.{owner} = :owner AND {alias}.table_name IN ({placeholders})"

        with oracledb.connect(user=settings.user, password=settings.password, dsn=settings.dsn,
                              tcp_connect_timeout=settings.connect_timeout_seconds) as conn:
            conn.call_timeout = settings.call_timeout_ms
            with conn.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")

                def query(sql):
                    cursor.execute(sql, binds)
                    names = [item[0].lower() for item in cursor.description]
                    rows = cursor.fetchmany(MAX_METADATA_ROWS + 1)
                    if len(rows) > MAX_METADATA_ROWS:
                        raise SchemaError("Schema metadata exceeds the supported catalog size")
                    return [dict(zip(names, row)) for row in rows]

                tables = query(f"""
                    SELECT t.table_name, c.comments
                    FROM all_tables t LEFT JOIN all_tab_comments c
                      ON c.owner = t.owner AND c.table_name = t.table_name AND c.table_type = 'TABLE'
                    WHERE {scope('t')} ORDER BY t.table_name
                """)
                catalog = {row["table_name"]: {
                    "owner": settings.schema, "table": row["table_name"], "comments": row["comments"],
                    "columns": [], "constraints": [], "indexes": [], "relationships": [],
                } for row in tables}
                columns = query(f"""
                    SELECT c.table_name, c.column_name, c.column_id AS position, c.data_type,
                           c.data_length AS length_bytes, c.char_length, c.char_used,
                           c.data_precision AS precision, c.data_scale AS scale, c.nullable, m.comments
                    FROM all_tab_columns c LEFT JOIN all_col_comments m
                      ON m.owner = c.owner AND m.table_name = c.table_name AND m.column_name = c.column_name
                    WHERE {scope('c')} ORDER BY c.table_name, c.column_id
                """)
                for row in columns:
                    table = row.pop("table_name")
                    if table in catalog:
                        row["nullable"] = row["nullable"] == "Y"
                        catalog[table]["columns"].append(row)
                constraints = query(f"""
                    SELECT c.table_name, c.constraint_name, c.constraint_type, c.status, c.validated,
                           c.search_condition_vc AS condition, cc.column_name, cc.position
                    FROM all_constraints c LEFT JOIN all_cons_columns cc
                      ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name
                    WHERE {scope('c')}
                    ORDER BY c.table_name, c.constraint_name, cc.position
                """)
                grouped = {}
                for row in constraints:
                    table = row["table_name"]
                    if table not in catalog:
                        continue
                    key = (table, row["constraint_name"])
                    if key not in grouped:
                        item = {name: row[name] for name in
                                ("constraint_name", "constraint_type", "status", "validated", "condition")}
                        # SEARCH_CONDITION_VC can be truncated by Oracle at 4000 bytes.
                        item.update(columns=[], condition_may_be_truncated=bool(
                            row["condition"] and len(row["condition"].encode("utf-8")) >= 4000))
                        grouped[key] = item
                        catalog[table]["constraints"].append(item)
                    if row["column_name"] is not None:
                        grouped[key]["columns"].append({"column": row["column_name"], "position": row["position"]})
                indexes = query(f"""
                    SELECT i.table_name, i.index_name, i.index_type, i.uniqueness, i.status,
                           c.column_name, c.column_position AS position, c.descend AS direction
                    FROM all_indexes i JOIN all_ind_columns c
                      ON c.index_owner = i.owner AND c.index_name = i.index_name
                      AND c.table_owner = i.table_owner AND c.table_name = i.table_name
                    WHERE {scope('i', 'table_owner')}
                    ORDER BY i.table_name, i.index_name, c.column_position
                """)
                grouped = {}
                for row in indexes:
                    table = row["table_name"]
                    if table not in catalog:
                        continue
                    key = (table, row["index_name"])
                    if key not in grouped:
                        item = {name: row[name] for name in ("index_name", "index_type", "uniqueness", "status")}
                        item["columns"] = []
                        grouped[key] = item
                        catalog[table]["indexes"].append(item)
                    grouped[key]["columns"].append({"column": row["column_name"], "position": row["position"],
                                                     "direction": row["direction"]})
                relationships = query(f"""
                    SELECT f.table_name, f.constraint_name, f.status, f.validated, f.delete_rule,
                           p.table_name AS referenced_table, fc.position,
                           fc.column_name, pc.column_name AS referenced_column
                    FROM all_constraints f
                    JOIN all_constraints p ON p.owner = f.r_owner AND p.constraint_name = f.r_constraint_name
                    JOIN all_cons_columns fc ON fc.owner = f.owner AND fc.constraint_name = f.constraint_name
                    JOIN all_cons_columns pc ON pc.owner = p.owner AND pc.constraint_name = p.constraint_name
                                              AND pc.position = fc.position
                    WHERE f.constraint_type = 'R' AND {scope('f')} AND {scope('p')}
                    ORDER BY f.table_name, f.constraint_name, fc.position
                """)
                grouped = {}
                for row in relationships:
                    table = row["table_name"]
                    if table not in catalog or row["referenced_table"] not in catalog:
                        continue
                    key = (table, row["constraint_name"])
                    if key not in grouped:
                        item = {name: row[name] for name in ("constraint_name", "status", "validated", "delete_rule", "referenced_table")}
                        item.update(table=table, owner=settings.schema, referenced_owner=settings.schema, column_pairs=[])
                        grouped[key] = item
                        catalog[table]["relationships"].append(item)
                    grouped[key]["column_pairs"].append({"position": row["position"], "column": row["column_name"],
                                                          "referenced_column": row["referenced_column"]})
                return catalog


def _tokens(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    return words | {word[:-1] for word in words if len(word) > 3 and word.endswith("s")}


class SchemaCatalog:
    def __init__(self, settings: OracleSettings, *, loader: Callable[[], dict] | None = None,
                 clock: Callable[[], float] = monotonic):
        self.settings = settings
        self._loader = loader or OracleMetadataLoader(settings).load
        self._clock = clock
        self._lock = RLock()
        self._snapshot = None
        self._expires = 0.0

    def _refresh(self):
        # Caller holds the lock. Build in isolation; never expose half an index.
        try:
            tables = self._loader()
            if set(tables) - set(self.settings.allowed_tables):
                raise SchemaError("Catalog contains unauthorized tables")
            version = hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest()
            documents, index = [], {}
            for name, table in sorted(tables.items()):
                candidates = [{"kind": "table", "table": name, "column": None, "comments": table["comments"]}]
                candidates.extend({"kind": "column", "table": name, "column": col["column_name"],
                                   "data_type": col["data_type"], "comments": col["comments"]}
                                  for col in table["columns"])
                for doc in candidates:
                    doc_id = len(documents)
                    documents.append(doc)
                    for token in _tokens(" ".join(str(v) for v in doc.values() if v is not None)):
                        index.setdefault(token, set()).add(doc_id)
            self._snapshot = {"tables": deepcopy(tables), "documents": documents, "index": index,
                              "schema_version": version,
                              "refreshed_at": datetime.now(timezone.utc).isoformat()}
            self._expires = self._clock() + self.settings.schema_cache_ttl_seconds
        except Exception as exc:
            self._snapshot = None  # Do not silently serve old metadata after a failed refresh.
            logger.warning("Schema discovery failed (%s)", type(exc).__name__)
            raise SchemaError("Oracle schema discovery is unavailable; check configuration and grants.") from None

    def _current(self):
        with self._lock:
            if self._snapshot is None or self._clock() >= self._expires:
                self._refresh()
            return self._snapshot

    def _metadata(self, snapshot):
        return {"owner": self.settings.schema, "schema_version": snapshot["schema_version"],
                "refreshed_at": snapshot["refreshed_at"], "cache_ttl_seconds": self.settings.schema_cache_ttl_seconds}

    def _table_name(self, name: str) -> str:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", name):
            raise ValueError("Table must be a simple unquoted identifier")
        name = name.upper()
        if name not in self.settings.allowed_tables:
            raise ValueError("Table is not authorized for schema discovery")
        return name

    def list_tables(self) -> dict:
        snapshot = self._current()
        tables = [{"table": name, "comments": table["comments"], "column_count": len(table["columns"])}
                  for name, table in sorted(snapshot["tables"].items())]
        return dict(self._metadata(snapshot), status="found" if tables else "empty", tables=tables)

    def describe_table(self, table: str) -> dict:
        name = self._table_name(table)
        snapshot = self._current()
        description = snapshot["tables"].get(name)
        return dict(self._metadata(snapshot), status="found" if description else "not_found",
                    table=deepcopy(description))

    def get_relationships(self, table: str | None = None) -> dict:
        name = self._table_name(table) if table is not None else None
        snapshot = self._current()
        if name is not None and name not in snapshot["tables"]:
            return dict(self._metadata(snapshot), status="not_found", relationships=[])
        relationships = [rel for item in snapshot["tables"].values() for rel in item["relationships"]
                         if name is None or name in (rel["table"], rel["referenced_table"])]
        return dict(self._metadata(snapshot), status="found" if relationships else "empty",
                    relationships=deepcopy(relationships))

    def search_schema(self, query: str, limit: int = 10) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 400:
            raise ValueError("Schema search must contain 1..400 characters")
        tokens = _tokens(query)
        if not tokens:
            raise ValueError("Schema search must contain letters or numbers")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Schema search limit must be 1..50")
        snapshot = self._current()
        scores = {}
        for token in tokens:
            for doc_id in snapshot["index"].get(token, ()):
                scores[doc_id] = scores.get(doc_id, 0) + 1
        ranked = sorted(scores, key=lambda i: (-scores[i], i))
        results = [dict(snapshot["documents"][i], score=scores[i]) for i in ranked[:limit]]
        return dict(self._metadata(snapshot), status="found" if results else "no_matches",
                    matches=results, total_matches=len(ranked), truncated=len(ranked) > limit)

    def refresh_schema_index(self) -> dict:
        with self._lock:
            self._refresh()
            return dict(self._metadata(self._snapshot), status="refreshed", table_count=len(self._snapshot["tables"]))
