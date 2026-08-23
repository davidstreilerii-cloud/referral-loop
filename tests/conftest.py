"""Keep the referral suite out of the installed audit database.

`referral_loop/immutable_audit.py` resolves `AUDIT_DB` from its own package
location, so without this every audited action in this suite -- every
acknowledgement, every dismissal, every one of the several dozen `load_pack`
calls in test_pack.py -- would append to the repository's real
`data/audit_trail.db`. That database is append-only by construction, so those
rows could not be removed afterwards: the isolation has to hold on the first
write, not be cleaned up after it.

**Deliberately not `monkeypatch`.** pytest hands every fixture and the test body
the *same* function-scoped monkeypatch instance, and six tests in this package
call `monkeypatch.undo()` mid-test to take their own patch off. That undoes
every patch on the instance, including this one -- and three merge tests then
run `merge_patient` with `AUDIT_DB` pointing back at the installed database.
Measured, not theorised: it wrote three rows per suite run before this fixture
was rewritten to save and restore the value itself.
"""
import pytest

from referral_loop import audit

# Read once, at collection, before anything has redirected it. This is the path
# a real install writes to, and test_audit asserts against it.
INSTALLED_AUDIT_DB = str(audit._module().AUDIT_DB)


@pytest.fixture(autouse=True)
def isolated_audit_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("audit") / "audit_trail.db"
    module = audit._module()
    previous = module.AUDIT_DB
    previous_init = audit._initialised_for
    previous_failures = audit._write_failures

    module.AUDIT_DB = str(path)
    # audit.py caches which path it has initialised; the assignment above goes
    # behind that cache, so it is cleared here and again on the way out.
    audit._initialised_for = None
    # The dropped-write count is process-global by design -- an operator wants
    # one number for the process, not one per request -- which makes it state a
    # test can see from the test before it.
    audit._write_failures = 0
    try:
        yield path
    finally:
        module.AUDIT_DB = previous
        audit._initialised_for = previous_init
        audit._write_failures = previous_failures
