# referral-loop — Build Log

## 2026-08-20 — release baseline

Measured suite state:

```
1805 passed, 2 skipped, 11 deselected in 826.78s (0:13:46)
TOTAL                     4455    207    95%
Required test coverage of 90.0% reached. Total coverage: 95.35%
```

Command (exit code 0):

```bash
REFERRAL_THRESHOLDS_ACCEPTED=1 python -m pytest tests/ -q --no-header \
  -m "not docker" --timeout=120 --cov=referral_loop --cov-report=term-missing
```

**Running the suite — three things worth knowing:**

- Use the invocation above, not bare `pytest`. `tests/test_install_closure.py` is
  `@pytest.mark.docker` and requires a Docker daemon; those are the 11 deselected tests. The
  `image` CI job covers them against a real built image.
- Expect roughly 14 minutes. `test_spec_12_and_13_the_whole_suite_runs_under_both_guards`
  re-runs the whole suite in a guarded subprocess and dominates the wall clock. That is by design.
- Never run with a per-test timeout below 120s, or the guarded meta-test above fails spuriously.

**Coverage:** the floor is `fail_under = 90` in `pyproject.toml`, deliberately set five points
under the measured value as platform margin rather than slack. `encryption_check.py` has
per-platform OS-detection branches, so a Linux runner covers a different subset than win32. A CI
figure between 90 and 95 is that difference, not a regression — check which lines moved before
adjusting the floor.
