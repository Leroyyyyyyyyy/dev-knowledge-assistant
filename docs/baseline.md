# M0 基线：原 RAG 检索层复现

复现日期：2026-10-05。只测检索层，不调用 LLM。

## 被复现的对象

| 项 | 值 |
| --- | --- |
| 检索代码 | 上游 `agenticloops-ai/agentic-ai-engineering`，`01-foundations/06-codebase-navigator/`（`indexer/`、`store/`），MIT |
| 上游 commit | `401dc0f7f1fa09ac09eebbd7dacc04d1c34bd743`，**工作区有未提交改动**（见下方「已知问题」） |
| 切块改动 | `agent-engineering-log/rag-eval/patches/chunker.patch`，已应用在上游工作区，未提交 |
| 评测脚本 | `agent-engineering-log/rag-eval/`，仓库 commit `bd26fdf40a87a86fbe6e007fcb70714a6b78ffd6` |
| 评测集 | `rag-eval/eval_set.json`：60 条中文查询，`queries_en.json`：同 60 条英文版 |
| 语料 | 上游仓库的 `01-foundations/` 目录（**工作区**内容） |
| collection | `local-01-foundations`，213 个 chunk |
| Embedding | `sentence-transformers/all-MiniLM-L6-v2`，本地，MPS |
| 向量库 | ChromaDB，本地持久化 |
| Reranker | cross-encoder `ms-marco-MiniLM`，候选池 `RERANK_TOP_N=20` |
| Python | 3.13.14（上游 navigator 的 `.venv`，`uv sync --python 3.13`） |

## 评测集结构

60 条，4 类，每类 15 条：

- `exact_identifier` / `semantic` / `value_lookup`：`expect_any`，按 **hit@k** 计（top-k 里命中任一备选字符串）。n=45。
- `cross_file`：`expect_all`，按 **coverage@k** 计（命中数 / 应命中数）。n=15。

ground truth 是代码字符串，不是行号。跑前脚本会自检 60 条 ground truth 都能在语料里找到。

## 结果

### 中文查询，纯向量（`run_eval.py`）

| 指标 | @1 | @3 | @5 | @10 |
| --- | --- | --- | --- | --- |
| hit@k（n=45） | 36% | 47% | 49% | 53% |
| coverage@k（n=15） | 0% | 4% | 4% | 7% |

按类别 @5：exact_identifier 93%，semantic 19%，value_lookup 36%，cross_file 4%。

### 中英对照（`compare_lang.py`，hit 与 coverage 混合平均）

| 配置 | 语言 | @1 | @3 | @5 | @10 |
| --- | --- | --- | --- | --- | --- |
| 纯向量 | 中文 | 27% | 36% | 38% | 42% |
| 纯向量 | 英文 | 36% | 59% | **76%** | 81% |
| + rerank | 中文 | 18% | 35% | 37% | 41% |
| + rerank | 英文 | **52%** | 72% | **78%** | 85% |
| hybrid（BM25+RRF，默认权重 1:1） | 中文 | 28% | 33% | 37% | 43% |
| hybrid（BM25+RRF，默认权重 1:1） | 英文 | 35% | 57% | **64%** | 72% |

英文按类别 @5：

| 类别 | 纯向量 | + rerank | hybrid |
| --- | --- | --- | --- |
| exact_identifier | 100% | 100% | 100% |
| semantic | 81% | 88% | 81% |
| value_lookup | 79% | 79% | 43% |
| cross_file | 46% | 47% | 30% |

### 与历史记录的对照

`rag-eval/README.md` 记录的数字：

- 英文纯向量 @5 = 76%：**复现一致**。
- rerank 英文 @5 76% → 78%，@1 36% → 52%：**复现一致**。
- 混合检索英文 @5「最好 68%」：本次只跑了默认权重，得 64%。68% 来自调过的权重（README 写「九组参数无一胜过纯向量」），本次没有逐组复跑。
- chunker patch「recall@5 54% → 57%，英文 86% → 93%」：本次**没有对应数字**。这组数是评测集扩到 60 条、指标拆成 hit/coverage 之前记的（git log：`87b34d9` 扩集、`9b719d8` 换 coverage），口径不同，不能和本表直接比。

### 索引重建一致性

先在已有索引上跑一遍，再 `reindex.py` 删 collection 全量重建后跑一遍：中英文 60 条**逐条排名完全一致**。说明已有索引就是当前 chunker + 当前语料的产物。

## 已知问题（影响后续里程碑）

1. **语料没有绑定 commit。** 索引读的是上游工作区：`01-foundations/` 下有 21 个已修改文件和 1 个未跟踪文件（`05-agent-loop/my_agent.py`），全部进了索引。spec §4.1 要求按 commit 索引，M1 必须改成从指定 commit 读取（例如 `git archive` 或 `git worktree`）。
2. **中文查询配英文 embedding 模型。** MiniLM-L6-v2 是英文模型，中文 @5 只有 38%，比英文低 39 个百分点。rerank 对中文 @1 还是负的（27% → 18%）。如果演示用户用中文提问，这是最大的短板，需要在「换多语言 embedding」和「查询翻译」之间做单变量实验。
3. **cross_file 很弱。** 英文 coverage@5 只有 46–47%，多文件问题是答案质量的主要风险。
4. `uv run` 每次会按 `uv.lock` 同步 `.venv`（本次输出 `Uninstalled 3 packages / Installed 3 packages`），说明 `.venv` 之前和锁文件有偏差。新仓库要用自己的锁文件。

## 复现命令

目录布局：`agent-engineering-log/` 与 `agentic-ai-engineering/` 并列，上游已应用 `chunker.patch`，navigator 已 `uv sync --python 3.13`。

```bash
cd agent-engineering-log/rag-eval
NAV=../../agentic-ai-engineering/01-foundations/06-codebase-navigator
uv run --directory $NAV python "$PWD/reindex.py"
uv run --directory $NAV python "$PWD/run_eval.py"
uv run --directory $NAV python "$PWD/compare_lang.py"
RERANK=1 uv run --directory $NAV python "$PWD/compare_lang.py"
RETRIEVER=hybrid uv run --directory $NAV python "$PWD/compare_lang.py"
```

全部不需要 API key。Ragas 相关脚本（`run_ragas.py`、`run_ragas_llm.py`）需要模型 key，本次没跑。

## 中文查询实验（2026-10-05）

脚本：`evals/experiments/zh_retrieval.py`（本仓库，使用复制进来的 `app/retrieval/`）。逐条结果：`evals/reports/zh_retrieval_2026-10-05.json`。只测检索层，不加 rerank，不调用 LLM。

**复制验证**：本仓库环境（`pyproject.toml` 锁定和上游同版本的 chromadb 1.4.1、sentence-transformers 5.2.2、torch 2.10.0、numpy 1.26.4）+ 复制的 chunker 跑 MiniLM，中文 27/36/38/42%、英文 36/59/76/81%，四个类别 @5 也和上表完全一致。这说明复制没有改变检索行为（只核对了聚合数，没有逐条比较）。

查询模式：`zh` 原中文；`en` 人工英文（翻译的上限）；`mt` 用本地 `Helsinki-NLP/opus-mt-zh-en` 整句翻译；`mt_keep` 同一翻译模型，只翻中文片段，ASCII 片段（标识符）原样保留。翻译结果已提交在 `evals/datasets/foundations60/queries_mt*_opus.json`。

| embedding | max_seq_len | 查询 | @1 | @3 | @5 | @10 |
| --- | --- | --- | --- | --- | --- | --- |
| all-MiniLM-L6-v2（基线） | 256 | zh | 27% | 36% | 38% | 42% |
| | | en | 36% | 59% | 76% | 81% |
| | | mt | 37% | 56% | 69% | 79% |
| | | mt_keep | 41% | 61% | 76% | 84% |
| paraphrase-multilingual-MiniLM-L12-v2 | 128 | zh | 24% | 35% | 54% | 63% |
| | | mt_keep | 29% | 44% | 56% | 74% |
| multilingual-e5-base | 512 | zh | 50% | 65% | 71% | 76% |
| | | mt_keep | 47% | 61% | 72% | 79% |
| **bge-m3** | 8192 | **zh** | **53%** | **71%** | **80%** | **87%** |
| | | en | 54% | 77% | 86% | 87% |
| | | mt | 46% | 62% | 76% | 83% |
| | | mt_keep | 46% | 63% | 78% | 83% |

按类别 @5（中文用户相关的两条主线）：

| 配置 | exact_identifier | semantic | value_lookup | cross_file |
| --- | --- | --- | --- | --- |
| MiniLM + zh | 93% | 19% | 36% | 4% |
| MiniLM + mt_keep | 93% | 75% | **79%** | 57% |
| bge-m3 + zh | 100% | **94%** | 64% | **59%** |

### 结论

1. **选 bge-m3，中文查询直接检索，不翻译。** @5 80%（基线 38%），@1 53%（基线 27%），已经超过旧方案的英文查询（MiniLM + en，@5 76%）。
2. 翻译方案的上限是 76%，「保留标识符」的分段翻译已经达到这个上限，但给 bge-m3 加翻译反而变差（80% → 78%、@1 53% → 46%）。多语言模型直接吃中文时，翻译只会引入噪声。
3. 整句机器翻译会改写标识符（`chunk_python` → `cunk_python`、`collection` → `Collaction`），exact_identifier 从 93% 掉到 87%。只翻中文片段后恢复。
4. 不是所有多语言模型都有效：paraphrase-multilingual-MiniLM-L12 只有 54%，max_seq_length=128 截断了大部分代码块，这一项和模型能力混在一起，不能单独归因。
5. bge-m3 的弱项是 value_lookup（64%，不如 MiniLM + mt_keep 的 79%），以后建新评测集时重点看。

### 限制

- n=60，每条 ≈ 1.7 个百分点。80% 对 76% 只差约 2–3 条，@1 53% 对 41% 差约 7 条，后者更可信。
- 这是反复调参用过的开发集，结论要在新语料的新评测集上复核。
- 成本：bge-m3 索引 213 个 chunk 用 107s（MiniLM 2s），模型约 1GB，向量 1024 维。新语料体量小，可以接受；M4 全量重建时要算进去。
- 没测 rerank。现有 `ms-marco-MiniLM` 是英文 reranker，配中文查询之前就是负效果；要试应换多语言 reranker（如 `bge-reranker-v2-m3`），单独作为一个变量。

## 演示语料检索评测：demo20（2026-10-05）

脚本 `evals/experiments/demo20_retrieval.py`，评测集 `evals/datasets/demo20/eval_set.json`（v1，20 题，冻结于 2026-10-05），逐题结果 `evals/reports/demo20_retrieval_2026-10-05.json`。只测检索层，不加 rerank。

- 语料：`config/repos.json` 里的两个仓库，用 `git archive` 从固定 commit 导出（fieldops `1b5fe9c`，排除了待删的 `docs/interview-notes.md`；mineops `85f5da0`）。完整文件集共 270 个 chunk（fieldops 157，mineops 113）。
- 判定：chunk 与证据范围同仓库、同路径、行号有重叠就算命中。只有一组证据的题按 hit@k 算，多组按 coverage@k 算。d20 没有证据（不可回答题），不计分，所以 n=19。
- 运行前会核对每个证据范围：文件存在、行号不越界、锚点文本在范围内。20 题全部通过。

| 配置 | @1 | @3 | @5 | @10 |
| --- | --- | --- | --- | --- |
| MiniLM + zh | 0% | 0% | 0% | 0% |
| MiniLM + mt_keep | 8% | 37% | 55% | 68% |
| e5-base + zh | 3% | 8% | 13% | 13% |
| bge-m3 + zh | **21%** | 45% | 47% | 68% |
| bge-m3 + zh，旧文件集 | 21% | 45% | 45% | 68% |
| bge-m3 + mt_keep | 5% | **53%** | **63%** | **76%** |
| bge-m3 + zh，去掉中文手册 | **26%** | 47% | 53% | **76%** |
| MiniLM + mt_keep，去掉中文手册 | 8% | 42% | 55% | 74% |

### 发现

1. **中文文档吸走了中文查询。** mineops 的 `docs/LEARNING_GUIDE.zh-CN.md` 是语料里唯一的中文长文档。用中文查询时，MiniLM 的 top-1 19/19 是它，e5 是 18/19，bge-m3 是 3/19；bge-m3 的 top-5 里它也占了 27/100。读过被检索到的那几段：手册指对了文件和概念（例如第 3 课写了「使用 event_id 实现幂等」），但没有给出实际机制，属于相关的指引，不是证据，所以标注没有改。去掉手册后，bge-m3 @5 47% → 53%，@1 21% → 26%，约等于 1 题。
2. **旧语料的结论没有复现。** 在 foundations60 上，bge-m3 加翻译变差（@5 80% → 78%）；在这里，bge-m3 加翻译 @5 47% → 63%、@10 68% → 76%，但 @1 从 21% 掉到 5%。两套语料的差别在于：这里有一份中文文档，翻译成英文后就不会被它吸走；而 foundations 里没有这样的文档。
3. **补扩展名的效果只有 1 题。** 完整文件集和旧文件集只在 d03 上不同：Makefile 能被检索到了（rank 4），所以 @5 从 45% 变成 47%。d02 的关键证据 `pyproject.toml` 在所有配置下都没进 top-10，**补扩展名只是让它能被检索，没让它被检索到**。
4. **有三题和模型无关，所有配置都失败**：d02（pyproject 的 marker 配置）、d08（跨仓库题：两个仓库各要命中一处，从来没有同时命中）、d18（有歧义的「怎么跑起来」，只有零星命中）。这三题要靠检索策略解决（按仓库分别检索、查询改写），换模型解决不了。
5. bge-m3 编码时每批 8 块会让 MPS 显存吃紧：最长的块有 2716 个 token，同批会补齐到这个长度，建索引用了 580s，下一条查询直接卡死在 MPS → CPU 拷贝上。改成每批 1 块后 43s 跑完，块不截断，向量内容不变。

### 结论与限制

- **n=19，每题约 5 个百分点。** 47%、53%、55%、63% 之间只差 1–3 题，**不足以选出新方案**。下面两条是排除掉的方案，有把握；其余都还只是方向。
- 有把握的：MiniLM 和 e5-base 不能直接用中文查询（0% 和 13%）。
- 还没定的：bge-m3 + zh 的 @1 最好，bge-m3 + mt_keep 的 @5 和 @10 最好。排在第一的片段会被模型最先读到，而覆盖面决定答案完整不完整，要等回答层评测才能决定哪个更重要。
- 当前暂定 **bge-m3 + zh**：@1 最好，链路里少一个翻译模型。翻译方案保留，作为下一轮对照。
- 评测集是助手起草的，19 题里 8 题是代码定位，题型分布不代表真实用户。
