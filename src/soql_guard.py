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
try:
    import sql_like_guard
except ImportError:
    from src import sql_like_guard

_DEFAULT_LIMIT = sql_like_guard.DEFAULT_LIMIT
_MAX_LIMIT = sql_like_guard.MAX_LIMIT


class SoqlRejected(Exception):
    pass


def validate_and_cap(soql: str) -> str:
    """Raises SoqlRejected if the query isn't a plain SELECT, or contains DML
    keywords or statement separators. Appends or caps LIMIT otherwise."""
    return sql_like_guard.validate_and_cap(soql, SoqlRejected, check_dml=True)


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
