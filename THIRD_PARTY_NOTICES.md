# Third-party notices

## agentic-ai-engineering (MIT)

`app/retrieval/chunker.py`, `app/retrieval/embedder.py` and `app/retrieval/vector_store.py` are copied from
[agenticloops-ai/agentic-ai-engineering](https://github.com/agenticloops-ai/agentic-ai-engineering),
`01-foundations/06-codebase-navigator/` at commit `401dc0f7f1fa09ac09eebbd7dacc04d1c34bd743`.

Changes from upstream:

- `chunker.py` includes the semantic-boundary patch from the author's `agent-engineering-log/rag-eval/patches/chunker.patch`
  (split on module-level constants, skip `repos/` and `data/`).
- All three files use stdlib `logging` instead of the upstream `common.logging_config` package.
- `chunker.py` indexes more file types (config, build and schema files). The upstream set is kept as
  `LEGACY_EXTENSIONS`. The skip-directory check now looks only at path parts inside the repository.

```
MIT License

Copyright (c) 2026 AgenticLoops AI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
