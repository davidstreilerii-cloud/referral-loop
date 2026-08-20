# referral-loop — Build Log

## 2026-08-20 — public release baseline

**Measured suite state** (the authoritative pre-release numbers; supersedes every prior figure):

```
1805 passed, 2 skipped, 11 deselected in 826.78s (0:13:46)
TOTAL                     4455    207    95%
Required test coverage of 90.0% reached. Total coverage: 95.35%
```

Command:

```bash
REFERRAL_THRESHOLDS_ACCEPTED=1 ./.venv/Scripts/python.exe -u -m pytest tests/ -q --no-header \
  -m "not docker" --timeout=120 --cov=referral_loop --cov-report=term-missing -rf
```

Exit code 0.

**Notes for anyone reading a different number elsewhere:**

- A prior record of "1048 passing / 13 failing" was a snapshot of the *extraction moment*
  (2026-08-01), not of this repo. It is stale and should not be cited.
- A prior record of "1,768 passing" (2026-08-06) was correct at the time; the suite has since
  grown to 1,805. `pyproject.toml` `[tool.coverage.report]` independently records 95.24% measured
  on the same invocation, consistent with today's 95.35%.
- Run the CI invocation, never bare `pytest`. `tests/test_install_closure.py` is
  `@pytest.mark.docker` and needs a Docker daemon; the 11 deselected tests are that file.
- The run takes ~14 minutes. `test_spec_12_and_13_the_whole_suite_runs_under_both_guards`
  re-runs the whole suite in a guarded subprocess and dominates the wall clock. This is by
  design — do not "fix" it, and do not run the suite with a timeout below 120s.

**Docs pass:** commit `d5f31e7` redacted `docs/` ahead of publication — 178 absolute paths
replaced with placeholders, internal strategy references removed. That commit cleaned the working
tree only; the history rewrite covers the same ground and is scrubbed in Task 3 of
`docs/superpowers/plans/2026-08-20-public-release.md`.
