"""SOQL defense-in-depth filter (PRD §9): rejects anything that isn't a
plain, LIMIT-capped SELECT before it reaches Salesforce. This is NOT the
access-control boundary — the user's own OAuth-scoped Salesforce session is
(object/field-level security already applies upstream, so this tool cannot
see data the connected user's profile doesn't grant). This filter exists so
a prompt-injection-triggered call can't smuggle DML, a sub-select into
another object, or an unbounded scan through this specific tool — it's a
second layer, not the only one.

Needs its own security review before shipping (PRD §9, and this plan's Task
13) given full-parity scope now includes write tools alongside this read
path — flagged, not self-approved.
"""
import re

_DML_KEYWORDS = re.compile(r"\b(INSERT|UPDATE|DELETE|UPSERT|MERGE|UNDELETE)\b", re.IGNORECASE)
_MULTI_STATEMENT = re.compile(r";")
_SELECT_PREFIX = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_LIMIT_CLAUSE = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 2000


class SoqlRejected(Exception):
    pass


def validate_and_cap(soql: str) -> str:
    """Raises SoqlRejected if the query isn't a plain SELECT, or contains DML
    keywords or statement separators. Appends or caps LIMIT otherwise."""
    if _MULTI_STATEMENT.search(soql):
        raise SoqlRejected("multi-statement SOQL is not allowed")
    if not _SELECT_PREFIX.match(soql):
        raise SoqlRejected("only SELECT queries are allowed")
    if _DML_KEYWORDS.search(soql):
        raise SoqlRejected("DML keywords are not allowed in this tool")

    existing_limit = _LIMIT_CLAUSE.search(soql)
    if existing_limit:
        limit_value = int(existing_limit.group(1))
        if limit_value > _MAX_LIMIT:
            return _LIMIT_CLAUSE.sub(f"LIMIT {_MAX_LIMIT}", soql)
        return soql
    return f"{soql.rstrip().rstrip(';')} LIMIT {_DEFAULT_LIMIT}"


def _demo():
    assert validate_and_cap("SELECT Id, Name FROM Account").endswith(f"LIMIT {_DEFAULT_LIMIT}")
    assert validate_and_cap("SELECT Id FROM Account LIMIT 50") == "SELECT Id FROM Account LIMIT 50"
    assert validate_and_cap("SELECT Id FROM Account LIMIT 99999").endswith(f"LIMIT {_MAX_LIMIT}")

    for bad in [
        "INSERT INTO Account (Name) VALUES ('x')",
        "UPDATE Account SET Name='x'",
        "DELETE FROM Account",
        "SELECT Id FROM Account; DELETE FROM Account",
        "SELECT Id FROM Account WHERE Name = 'UPDATE me'",  # DML keyword inside a string literal — still rejected, defense-in-depth errs conservative
    ]:
        try:
            validate_and_cap(bad)
            assert False, f"should have rejected: {bad}"
        except SoqlRejected:
            pass

    print("soql_guard: all checks passed")


if __name__ == "__main__":
    _demo()
