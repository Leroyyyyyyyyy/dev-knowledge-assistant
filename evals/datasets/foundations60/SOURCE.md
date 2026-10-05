# foundations60

The author's existing 60-query retrieval set, copied unchanged from
`agent-engineering-log/rag-eval/` (`eval_set.json` last changed in commit `87b34d9`).

- Corpus: `01-foundations/` of agenticloops-ai/agentic-ai-engineering (an upstream working tree, **not pinned to a commit**, see `docs/baseline.md`).
- `eval_set.json`: 60 Chinese queries in 4 categories × 15. Ground truth is code strings, not line numbers.
- `queries_en.json`: hand-written English phrasings of the same 60 queries, same ground truth.

This set was used repeatedly while tuning the old retrieval. It is a development / regression set, not an
independent test set.
