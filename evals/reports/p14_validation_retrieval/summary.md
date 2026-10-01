# P14 evaluation summary

- Run: `P14-20260925T094413Z-d7fa9722`
- Commit: `1767cee0a5223799ccc77f95d055e6647cc8e263` (dirty=True)
- Split: `validation`; model: `qwen3.8-flash`
- Raw predictions and full configuration are in the adjacent JSON files.
- The tiny synthetic sample is descriptive, not a population estimate.

| Suite | Metric | Value | Support | Status |
| --- | --- | ---: | ---: | --- |
| retrieval | `retrieval.recall_at_5` | 1.000 | 3 | MEASURED |
| retrieval | `retrieval.mrr` | 1.000 | 3 | MEASURED |
| retrieval | `retrieval.ndcg_at_10` | 1.000 | 3 | MEASURED |
| retrieval | `retrieval.no_answer_accuracy` | — | 0 | NOT_APPLICABLE |
| retrieval | `retrieval.filter_violation_rate` | 0.000 | 25 | MEASURED |

## Suite execution

- retrieval: exit=0; report=F:\乌龙\code\vs_code\简历项目\evals\reports\p14_validation_retrieval\retrieval.json
