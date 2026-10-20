"""Shared core for soql_guard.py (Salesforce) and coql_guard.py (Zoho):
reject anything that isn't a plain, LIMIT-capped SELECT, with an optional
DML-keyword check — SOQL needs it (it's a read-capable query language that
sits alongside a separate DML API this filter shares no syntax with), COQL
doesn't (its grammar is SELECT-only by design, no DML syntax exists in it
at all). Each caller keeps its own exception class and docstring so a
SoqlRejected/CoqlRejected error stays unambiguous about which provider
rejected the query.
"""
import re

_MULTI_STATEMENT = re.compile(r";")
_SELECT_PREFIX = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_LIMIT_CLAUSE = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)
_DML_KEYWORDS = re.compile(r"\b(INSERT|UPDATE|DELETE|UPSERT|MERGE|UNDELETE)\b", re.IGNORECASE)
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000


def validate_and_cap(query: str, rejected_cls: type[Exception], check_dml: bool) -> str:
    if _MULTI_STATEMENT.search(query):
        raise rejected_cls("multi-statement query is not allowed")
    if not _SELECT_PREFIX.match(query):
        raise rejected_cls("only SELECT queries are allowed")
    if check_dml and _DML_KEYWORDS.search(query):
        raise rejected_cls("DML keywords are not allowed in this tool")

    existing_limit = _LIMIT_CLAUSE.search(query)
    if existing_limit:
        limit_value = int(existing_limit.group(1))
        if limit_value > MAX_LIMIT:
            return _LIMIT_CLAUSE.sub(f"LIMIT {MAX_LIMIT}", query)
        return query
    return f"{query.rstrip().rstrip(';')} LIMIT {DEFAULT_LIMIT}"
