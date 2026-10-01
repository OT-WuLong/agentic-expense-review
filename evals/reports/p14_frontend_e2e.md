# P14 browser E2E and persistence smoke

- Result: 4/4 Playwright tests; duplicate side effects: 0.
- These golden-case durations are diagnostic only; no overall P95 is claimed.

| Test | Browser result | Business status | Machine terminal | Machine ms |
| --- | --- | --- | --- | ---: |
| incorrect token stays on login and shows an error | passed | — | — | — |
| A rejects commute and keeps cited evidence after refresh | passed | BUSINESS_REJECTED | APPROVAL_FINALIZED | 46859 |
| B passes lodging with policy and city-tier evidence | passed | COMPLETED | APPROVAL_FINALIZED | 46606 |
| C interrupts for human review and resumes to a persisted decision | passed | COMPLETED | HUMAN_REVIEW_REQUESTED | 123617 |
