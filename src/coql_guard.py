"""COQL defense-in-depth filter, parallel to soql_guard.py's SOQL one:
rejects anything that isn't a plain, LIMIT-capped SELECT before it reaches
Zoho. COQL's grammar is SELECT-only by design (no INSERT/UPDATE/DELETE
syntax exists in COQL at all — unlike SOQL, which is read-capable but lives
alongside a separate DML API this filter has nothing to do with), so this
is not a DML-injection guard the way soql_guard.py is. It exists for the
same reason: a prompt-injection-triggered call shouldn't be able to send an
uncapped or multi-statement query through this specific tool with zero
local validation, even though Zoho's own COQL endpoint only ever returns
data scoped to the connected user's own tenant and permissions.

Security review finding, 2026-10-21: zoho_query_coql had no local
validation at all, unlike salesforce_query_soql's soql_guard.py.
"""
try:
    import sql_like_guard
except ImportError:
    from src import sql_like_guard

_DEFAULT_LIMIT = sql_like_guard.DEFAULT_LIMIT
_MAX_LIMIT = sql_like_guard.MAX_LIMIT


class CoqlRejected(Exception):
    pass


def validate_and_cap(coql: str) -> str:
    """Raises CoqlRejected if the query isn't a plain SELECT, or contains a
    statement separator. Appends or caps LIMIT otherwise."""
    return sql_like_guard.validate_and_cap(coql, CoqlRejected, check_dml=False)


def _demo():
    assert validate_and_cap("SELECT id, Last_Name FROM Contacts").endswith(f"LIMIT {_DEFAULT_LIMIT}")
    assert validate_and_cap("SELECT id FROM Contacts LIMIT 50") == "SELECT id FROM Contacts LIMIT 50"
    assert validate_and_cap("SELECT id FROM Contacts LIMIT 99999").endswith(f"LIMIT {_MAX_LIMIT}")

    for bad in [
        "SELECT id FROM Contacts; SELECT id FROM Leads",
        "UPDATE Contacts SET Last_Name = 'x'",  # not valid COQL syntax either way, still rejected defensively
        "DELETE FROM Contacts",
    ]:
        try:
            validate_and_cap(bad)
            assert False, f"should have rejected: {bad}"
        except CoqlRejected:
            pass

    print("coql_guard: all checks passed")


if __name__ == "__main__":
    _demo()
