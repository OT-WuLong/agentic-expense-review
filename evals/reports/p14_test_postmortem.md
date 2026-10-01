# P14 frozen-test postmortem

- Run: `P14-20260925T111538Z-a60ea863` on clean commit `080fe29`.
- Dataset: frozen `1.2.0`; one test execution only. Some test rows had been exposed during schema inspection before the run, so this is not a fully blind result.
- Outcome: `INVALID_RUN`; raw manifest, metrics, predictions, stdout and stderr remain in `p14_test_once/`.

Retrieval returned all relevant groups in the two answer-bearing queries (Recall@5 = 1.0, nDCG@10 = 0.925) and no out-of-scope candidates among 33 returned items. The one no-answer query was present, but no-answer handling was `NOT_MEASURED`; those partial figures do not make the full run valid.

All three non-golden approval samples ended `INSUFFICIENT_EVIDENCE`: strict task success 0/3, three-class recommendation accuracy 0/3, rule-result exact match 0/3. There were no automatic passes or system errors in this workflow subrun. The reference-snapshot policy documents did not have the required published rules in the current PostgreSQL catalog; the rule engine returned `RULE_CATALOG_ENTRY_MISSING`. The test also exposed retrieval/stopping gaps, but this run cannot be used to tune the same frozen split.

The security subrun exited before producing `security.json`: `tool_call_parameters` supplied a JSON object that the evaluator incorrectly treated as an attachment excerpt string. A separately constructed dev case now checks the proper tool-schema path. That harness fix was made **after** the frozen run; it does not retroactively validate the test or authorize rerunning dataset 1.2.0.

Before another final evaluation, derive/reference published rule fixtures from dev and validation policy sources, verify their dates and departments, exercise structured security payloads and no-answer queries outside test, then freeze a **new independent holdout and version**. Do not change these gold labels or the frozen thresholds to manufacture a pass. Golden A/B/C Playwright 4/4 and Fake/Stub recovery results remain separate functional evidence, not substitutes for the failed non-golden evaluation.
