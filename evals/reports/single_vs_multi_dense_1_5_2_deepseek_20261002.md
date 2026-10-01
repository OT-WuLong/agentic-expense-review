# Agentic ablation (validation)

- Status: `MEASURED`; commit: `149cdf6882ecad221d3c1194d8a5065c89eb0d9e` (dirty=True)
- Same synthetic samples and hard limits; one live run per variant.
- single_agent_dense uses one unified Agent prompt for planning and assessment, one retrieval round, and the same final rules. This compares the full workflow bundle, not agent count alone.
- Cost and exact duplicate-query rate are not measured.

| Variant | Business success | Strict success | Recommendation match | False auto-pass | System errors | Non-error P50 ms | Agent tokens* |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| single_agent_dense | 54/60 | 47/60 | 95.0% | 0.0% | 1.7% | 29432 | 613489 |
| multi_agent | 56/60 | 50/60 | 98.3% | 0.0% | 0.0% | 21416 | 655295 |

- Recommendation difference: +3.33 percentage points.
- Business success difference: +3.33 percentage points; paired wins/losses: 3/1.
- Business success requires correct suggestion, required final evidence, rule outcomes, risk flags and allowed tools; it excludes architecture-specific tool-call counts and stop labels.
- Synthetic pre-parsed attachment inputs; one run per variant, not a raw-PDF end-to-end or external generalisation score.

*Agent tokens are a lower bound; failed calls and embeddings are excluded.
