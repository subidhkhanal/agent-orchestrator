| Metric | research-memo | single-agent |
|---|---|---|
| Task success (judge + reference facts) | 0% | 0% |
| Judge mean score (1-5) | 2 | 2 |
| Citation validity (claims citing a real source) | 50% | 15% |
| Reference facts present | 0% | 100% |
| Avg cost per run (USD) | $0.0115 | $0.0126 |
| Avg tokens per run | 58,380 | 54,625 |
| Avg wall-clock per run (s) | 268.9 | 299.8 |
| Supervisor routing validity | 100% | n/a |
| Runs over max_usd | 0 | 0 |

Per category (successful / runs):

| Category | research-memo | single-agent |
|---|---|---|
| both | 0/1 | 0/1 |

- p95 node latency, research-memo: coder 48.64s, researcher 7.7s, reviewer 156.69s, supervisor 1.44s
- p95 node latency, single-agent: single_agent 299.83s

Run metadata: {"tasks": 1, "skipped_no_web_search_key": [], "models_config": "config/models.toml", "runs": 2, "budget": {"tokens": 60000, "usd": 0.05}, "date": "2026-09-29"}
