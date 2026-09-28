| Metric | research-memo | single-agent |
|---|---|---|
| Task success (judge + reference facts) | 50% | 0% |
| Judge mean score (1-5) | 3 | 1.5 |
| Citation validity (claims citing a real source) | 80% | 14% |
| Reference facts present | 100% | 100% |
| Avg cost per run (USD) | $0.0091 | $0.0249 |
| Avg tokens per run | 35,062 | 47,986 |
| Avg wall-clock per run (s) | 200.4 | 339.2 |
| Supervisor routing validity | 100% | n/a |
| Runs over max_usd | 0 | 0 |

Per category (successful / runs):

| Category | research-memo | single-agent |
|---|---|---|
| impossible | 0/1 | 0/1 |
| rag | 1/1 | 0/1 |

- p95 node latency, research-memo: coder 49.97s, researcher 54.7s, reviewer 95.44s, supervisor 6.32s
- p95 node latency, single-agent: single_agent 352.75s

Run metadata: {"tasks": 2, "skipped_no_web_search_key": [], "budget": {"tokens": 60000, "usd": 0.05}, "models_config": "config/models.toml", "date": "2026-09-28"}
