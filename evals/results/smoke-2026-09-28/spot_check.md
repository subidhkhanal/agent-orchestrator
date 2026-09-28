# Spot-check list

Read these by hand; the judge can be wrong.

## none-ceo / research-memo

- status: completed (supervisor_finished); success: False; judge: False score 1; facts: None; citation validity: None; cost $0.001793
- judge rationale: The system returned no memo, failing to explicitly state that the CEO profile cannot be derived from the provided documents. It did not acknowledge the unanswerable nature of the request.

```markdown
(no memo)
```

## none-ceo / single-agent

- status: failed (None); success: False; judge: False score 1; facts: None; citation validity: None; cost $0.031933
- judge rationale: The response provided no memo, thus did not explicitly state that the information is unavailable, failing the unanswerable task requirement.

```markdown
(no memo)
```

## rag-parental / research-memo

- status: completed (budget_exhausted); success: True; judge: True score 5; facts: True; citation validity: 0.8; cost $0.016434
- judge rationale: The memo accurately reports the primary and secondary caregiver leave lengths and conditions, all directly supported by the cited source, with no extraneous or unsupported statements.

```markdown
## Overview
Northwind Labs provides paid parental leave benefits as outlined in its 2026 Benefits Guide [doc_94e09c6b]. This memo summarizes the leave entitlements for primary and secondary caregivers.

## Paid Parental Leave
### Primary Caregivers
Primary caregivers are eligible for **18 weeks of fully paid parental leave**. This leave must be taken **within 12 months of the birth, adoption, or placement** of the child [doc_94e09c6b].

### Secondary Caregivers
Secondary caregivers receive **8 weeks of fully paid parental leave**. Like primary caregivers, this leave must be taken **within 12 months of the birth, adoption, or placement** of the child [doc_94e09c6b].

## Eligibility and Usage
- Leave must be taken within 12 months of the birth, adoption, or placement of the child [doc_94e09c6b].

## Open Questions
- None identified based on the available source material.

```

## rag-parental / single-agent

- status: completed (budget_exhausted); success: False; judge: False score 2; facts: True; citation validity: 0.1429; cost $0.017867
- judge rationale: The memo accurately cites the 18‑week and 8‑week parental leave figures, but it adds an unsupported claim about eligibility that is not present in the cited source.

```markdown
## Northwind Labs Paid Parental Leave

- **Primary caregivers** are entitled to **18 weeks** of fully paid parental leave.
- **Secondary caregivers** are entitled to **8 weeks** of fully paid parental leave.
- The leave must be taken within **12 months** of the birth, adoption, or placement of a child.

These provisions are described in the Northwind Labs Benefits Guide 2026 [doc_94e09c6b].

## Eligibility

The Benefits Guide indicates that the parental‑leave benefits apply to **eligible employees**, but the specific eligibility criteria (e.g., length of service) are not detailed in the available source.

## Open Questions

- What are the precise eligibility requirements (such as minimum tenure) for receiving paid parental leave at Northwind Labs?
- Are there any provisions for extending or modifying the leave under special circumstances?

```
