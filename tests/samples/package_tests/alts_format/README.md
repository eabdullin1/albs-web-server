# Fixtures in the format of the ALTS side

Copied from ALTS, branch `tmt-tests-stage`, commit `da16bc4`,
`tests/fixtures/tmt` (see the README there): what ALTS takes from ALBS and
posts back, as its flow tests check it. They describe the format the two
sides agreed on; the payloads and results captured on the test stand are
one level up.

| File | Source |
|---|---|
| `grep_al9_payload.json` | `grep-al9/payload.json`: a component task payload as ALTS reads it (`TaskRequestPayload`, every field) |
| `grep_al9_expected_result.json` | `grep-al9/expected_result.json`: the `result` ALTS posts for it, without `logs`: 25 pass, 2 fail in tier2-tier3, `tmt_exit_code` 1 |
| `e2e_plans_payload.json` | `e2e-plans/payload.json`: synthetic plans, `skipped_tests: [tmt_test_/e2e/skipme]`, an internal plan |
| `e2e_plans_result.json` | Not in ALTS as a file: put together from what `test_synthetic_plans` asserts about this result (keys `tmt_test_/e2e/pass@<plan>` of a test of two plans, a failure in `/plans/e2e/gating`, warn, two skipped tests, the internal plan excluded) and from `e2e-plans/results/*/results.yaml` (durations, notes). The `output` of a test is only its summary lines |
