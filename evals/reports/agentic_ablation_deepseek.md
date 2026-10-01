# Agentic ablation (validation)

- Status: `MEASURED`; commit: `1767cee0a5223799ccc77f95d055e6647cc8e263` (dirty=True)
- Same synthetic samples and hard limits; one live run per variant.
- Only multi_agent vs no_supervisor isolates the LLM Supervisor; other baselines also remove review stages.
- Cost and exact duplicate-query rate are not measured.

| Variant | Task success | Approval accuracy | Coverage | Steps | P95 ms | Agent tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| fixed_rag | 1/3 | 0.333 | 0.500 | 1.00 | 34202 | 0 |
| fixed_rewrite | 2/3 | 0.667 | 1.000 | 1.00 | 3309 | 0 |
| single_retrieval_agent | 1/3 | 0.667 | 1.000 | 1.00 | 12133 | 6377 |
| no_supervisor | 1/3 | 0.333 | 0.667 | 1.33 | 32449 | 19040 |
| multi_agent | 0/3 | 0.000 | 0.333 | 0.67 | 44183 | 8844 |
