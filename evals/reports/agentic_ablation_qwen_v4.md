# Agentic ablation (validation)

- Status: `MEASURED`; commit: `1767cee0a5223799ccc77f95d055e6647cc8e263` (dirty=True)
- Same synthetic samples and hard limits; one live run per variant.
- Only multi_agent vs no_supervisor isolates the LLM Supervisor; other baselines also remove review stages.
- Cost and exact duplicate-query rate are not measured.

| Variant | Task success | Approval accuracy | Coverage | Steps | Attempt P95 ms | System error rate | Agent tokens* |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| fixed_rag | 1/3 | 0.667 | 0.833 | 1.00 | 6966 | 0.000 | 0 |
| fixed_rewrite | 2/3 | 0.667 | 1.000 | 1.00 | 6610 | 0.000 | 0 |
| single_retrieval_agent | 1/3 | 0.667 | 0.833 | 1.00 | 12629 | 0.000 | 4216 |
| no_supervisor | 1/3 | 0.667 | 0.833 | 2.00 | 90526 | 0.000 | 14387 |
| multi_agent | 1/3 | 0.667 | 0.833 | 3.00 | 37865 | 0.000 | 17989 |

*Agent tokens are a lower bound; failed calls and embeddings are excluded.
