# 进度

每个里程碑结束时更新：已实现、如何验证、已知限制、下一步。只写实际跑过的东西。

## M0 盘点与复现（2026-10-05 完成）

### 已实现

- **原 RAG 已定位并复现**：上游 codebase-navigator 的检索代码和作者自建的 60 条评测集（foundations60），基线数字和历史记录一致（`docs/baseline.md`）。
- **检索模块复制进本仓库**：`app/retrieval/`（chunker、embedder、vector_store），保留 MIT 声明（`THIRD_PARTY_NOTICES.md`）。依赖版本锁定为和基线相同的 chromadb 1.4.1、sentence-transformers 5.2.2、torch 2.10.0、numpy 1.26.4，Python 3.13。
- **语料从 commit 读取**：`config/repos.json` 定义允许索引的仓库及其固定 commit，`app/indexing/snapshot.py` 用 `git archive` 导出，不读工作区。
- **chunker 改动**：补了配置和构建文件（`.toml`、`.ini`、`Makefile`、`Dockerfile` 等）；修了跳过目录时按绝对路径判断的 bug。上游的扩展名集合保留为 `LEGACY_EXTENSIONS`。
- **演示语料选定**：作者自己的两个公开仓库，fieldops-workorder-bridge 和 mineops。
- **需求**：三个种子问题（`docs/requirements.md`），由开发助手起草、作者认可，不来自试用者。
- **中文检索实验两轮**：foundations60（embedding 对比查询翻译）和 demo20（新语料上的 20 题）。

### 如何验证

```bash
uv sync --group experiments
# 复现基线：MiniLM 中文 @5 38%、英文 76%（复制后与 baseline.md 一致）
MODELS=minilm-l6 uv run --group experiments python -m evals.experiments.zh_retrieval
# 新语料评测：开跑前会核对 commit 和所有证据锚点
uv run --group experiments python -m evals.experiments.demo20_retrieval
```

两个脚本都需要把演示仓库 clone 在本仓库的上级目录（路径见 `config/repos.json`）；foundations60 还需要上游 agentic-ai-engineering 的工作区。

### 已知限制

- **embedding 方案只是暂定**：选了 bge-m3 + 中文查询。demo20 上 bge-m3 + 翻译的 @5 更高（63% 对 47%），但 @1 更低（5% 对 21%）。n=19，每题约 5 个百分点，区分不了。
- **demo20 评测集**由助手起草，19 道计分题里 8 道是代码定位，题型分布不代表真实用户。
- **三题所有配置都失败**：d02、d08、d18，原因和模型无关（`docs/baseline.md` 的 demo20 一节）。
- foundations60 的语料是上游工作区，没有绑定 commit。它只作为历史回归集使用。
- fieldops 的固定 commit 是 LICENSE PR 合并前的版本，`docs/interview-notes.md` 靠排除规则去掉。PR 合并后要换 commit，并重新核对 demo20 的标注。
- 没有测 rerank。没有真实用户试用。

### 下一步

M1：FastAPI 检索服务、固定索引版本、指向 commit 的引用。

## M1 检索服务与引用（2026-10-05，核心部分完成）

### 已实现

- **版本化索引构建**：`python -m app.indexing.build`。读取 `config/repos.json` 里每个仓库的固定 commit（`git archive`，不读工作区），建一个完整的新版本。只有写完并通过校验（chunk 数和记录一致、每个仓库都有内容），才在一个事务里把活动指针切过去。构建失败时，版本标记为 `failed`，删除写了一半的 collection，活动版本不变。数据库用部分唯一索引保证同一时间只有一个构建。
- **每个 chunk 的元数据**（spec §4.2）：`chunk_id`（`repo@commit12:path#Lx-Ly`）、`repo_id`、`commit_sha`、`index_version`、`path`、`start_line`/`end_line`、`content_hash`、`source_type`、`symbol`。
- **接口**：
  - `GET /health`：只返回 `{"status":"ok"}`。
  - `GET /ready`：检查数据库、活动索引（collection 的 chunk 数和记录一致）、编码模型和索引的模型一致。不调用任何付费模型，只返回每项通过与否。
  - `POST /api/retrieve`：Bearer 鉴权（常量时间比较）；`top_k` 有上限；请求体拒绝未知字段；可以按 `repo_id` 过滤。整个查询只读一次活动版本，之后都用这个版本。每次运行写入 `runs` 和 `run_chunks`，供 M2 校验引用。
- **引用**：后端拼成 `https://github.com/<owner>/<repo>/blob/<完整 commit>/<编码后路径>#Lx-Ly`，不用分支名，不接受短 sha。
- **错误约定**：所有错误都返回 `{code, message, request_id}`，`request_id` 同时出现在 `X-Request-ID` 头和日志里。鉴权失败 401，参数错误 422（`INVALID_REQUEST`、`UNKNOWN_REPO`），依赖不可用 503（`NO_ACTIVE_INDEX`、`INDEX_ENCODER_MISMATCH`、`RETRIEVAL_UNAVAILABLE`）。
- **修了一个引用行号的 bug**：上游 chunker 的行号没有扣掉被 strip 掉的首尾空行，文件末尾的换行还会多算一行。demo 语料 270 个块里有 219 个块的行号偏了。块内容没变，demo20 的所有排名也没变（已重跑核对）。

### 如何验证

```bash
uv run pytest                                   # 25 个测试：假编码器 + 临时 git 仓库
uv run python -m app.indexing.build             # 真实 bge-m3，两个演示仓库，约 33s
uv run uvicorn app.main:app --port 8077         # 需要 .env 里的 DKA_SERVICE_TOKEN
curl -s localhost:8077/ready
curl -s -X POST localhost:8077/api/retrieve -H "Authorization: Bearer $DKA_SERVICE_TOKEN" \
     -H "Content-Type: application/json" -d '{"query":"...","repo_id":"fieldops","top_k":5}'
```

自动测试覆盖：缺少或错误的 token、6 种非法输入、未知仓库、没有活动索引、编码模型不一致、索引只读 commit（工作区里未提交的改动不进索引）、exclude 生效、中文和空格路径的编码、按仓库过滤、运行记录和返回结果一致、构建失败不切换版本、并发构建被拒绝、块的行号和内容一致。

**2026-10-05 真实集成**：本机、真实 bge-m3，两个仓库共 270 个 chunk，构建并激活 1 个版本。`/ready` 返回 200。三个种子问题都返回 `ok`，延迟 45–130ms（MPS，单进程）。不带 token 返回 401。返回的第一个引用链接在 GitHub 上返回 200，对应行号的内容和返回的块逐字一致。

### 已知限制

- **检索质量和 demo20 的评测一致，没有改进**：Q1 的真正证据（TRANSITIONS、`_apply`）不在 top-5，Q2 仍然没命中，Q3 的 Makefile 排第 4。M1 只做服务化，没改检索。
- `no_evidence` 只表示一个结果都没返回。证据够不够回答问题，由回答层判断（spec §5），这里不用相似度阈值判断。
- 不传 `repo_id` 时，所有仓库放在一起按距离排序，**还没做按仓库分别检索再合并**（d08 那类跨仓库题需要）。
- 版本回滚、旧版本清理、删除文件的处理留到 M4。现在只会保留所有历史版本，没有回滚命令。
- 进程崩溃后，状态是 `building` 的记录会一直占着构建名额，需要手动改成 `failed`。M4 处理。
- 单进程、单实例；编码模型调用用一把锁串行化。
- 没有 `/api/answers`、`/api/feedback`，那是 M2 的事。

### 下一步

M2：Dify 问答流程。先做 `/api/answers`（只接受本次检索返回过的 chunk ID），再搭 Dify Chatflow 并导出 DSL。
