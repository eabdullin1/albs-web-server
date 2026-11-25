# Payloads and results of a real ALBS -> ALTS -> ALBS run

Captured on 2026-11-24 on a test stand running this branch together with
ALTS `tmt-tests-stage`, with a logging proxy between ALTS and ALBS, so the
JSON is exactly what went over the wire. Only the internal host names of the
stand were replaced (`pulp.example.org`, `git.example.org`,
`albs.example.org`); nothing else was edited.

The build was `grep` from `imports/c9/grep-3.6-5.el9`, AlmaLinux-9 x86_64,
with tmt enabled for AlmaLinux-9 x86_64.

| File | What |
|---|---|
| `payloads_grep.json` | `GET /api/v1/tests/get_test_tasks/` answer: three package test tasks and the component one, `tests/grep` from git.almalinux.org at `1531e51` |
| `grep_alts.yaml` | `alts.yaml` of `tests/grep` at that commit, without its `gating` key (every listed plan is required now) |
| `result_grep_package.json` | `POST /api/v1/tests/226/result/` of the `grep` package test task |
| `result_grep_component.json` | `POST /api/v1/tests/229/result/` of the component test task: 27 tmt tests of three plans, two fail in `/plans/tier2-tier3/public` |
| `payloads_e2e.json` | the same build retested with a copy of `tests/grep` that has `grep_e2e_alts.yaml` and three tests: `/e2e/pass` and `/e2e/skipme` in `/plans/e2e/gating`, `/e2e/fail` in `/plans/e2e/info` |
| `grep_e2e_alts.yaml` | `alts.yaml` of that copy without its `gating` key: `/e2e/skipme` is skipped. The plan names come from the run, when one of the plans didn't gate; now both are required |
| `result_e2e_component.json` | result of that component test task: `/e2e/skipme` was not run, `/e2e/fail` failed |
