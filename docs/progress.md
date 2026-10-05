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

## M2 Dify 问答

### 已完成：`POST /api/answers`（2026-10-05）

- **作用**：拿生成的答案，对照它所属的那次检索做校验，再保存。只能证明「引用真实存在，而且属于本次检索；正文里的链接都是后端签发的」，**不能证明答案的内容被引用支持**。后者由回答层评测检查。
- **输入**：`run_id`、`status`（`answered` / `insufficient_evidence`）、`answer_text`、`cited_chunk_ids`（最多 20 个）、`model`、`prompt_version`。
- **拒绝规则**（拒绝时返回 422，同时记为 `rejected` 保存，供评测用）：
  - `UNKNOWN_CITATION`：引用了不属于本次检索的 chunk，包括其他检索返回的真实 chunk。
  - `CITATION_REQUIRED`：`answered` 却没有任何引用。
  - `FABRICATED_LINK`：正文里的链接不是被引用 chunk 的规范链接，比如指向分支、指向未引用的 chunk、指向外站。
  - `DUPLICATE_CITATION`。
- **其他错误**：run 不存在 404；超过 60 分钟（`DKA_ANSWER_WINDOW_MINUTES`）410；同一个 run 已经接受了不同的答案 409。
- **重试**：内容完全相同的重复提交返回原来那条答案，不算冲突。拒绝之后可以再交一次修正版（对应 spec「最多一次格式修复」，次数由 Dify 控制）。数据库用部分唯一索引保证每个 run 只有一个被接受的答案。
- **数据库**：引入 `PRAGMA user_version` 迁移，v2 给 `run_chunks` 加上引用元数据，并新建 `answers` 表。M1 的旧数据库启动时会自动升级。

**验证**：`uv run pytest`，共 41 个测试，其中 16 个针对 answers：每条拒绝规则、别的检索返回的真实 chunk、过期、重试幂等、冲突、拒绝后再修正、鉴权、v1 → v2 迁移。

**2026-10-05 真实环境**：本机 v1 数据库启动时升级到 v2，原有的 4 条运行记录保留。真实检索之后：指向 main 分支的链接 → 422 `FABRICATED_LINK`；编造的 chunk ID → 422 `UNKNOWN_CITATION`；合规答案 → 200；同一答案重试 → 200，`answer_id` 相同。三次提交都写进了 `answers` 表。**这里的答案是手写的，还没有接真实的生成模型。**

### 已完成：`POST /api/feedback`（2026-10-05）

- **输入**：`run_id`、`resolved`（true/false）、`reason`（只在 `resolved=false` 时可填：`wrong_answer` / `incomplete` / `wrong_citation` / `not_relevant` / `other`）、`comment`（可选，最长 1000 字）。
- **关联**：run 必须存在（否则 404），而且必须有一条被接受的答案（否则 409 `NO_ANSWER_FOR_RUN`），因为反馈针对的是用户实际看到的答案。被拒绝的提交不算。
- **每个 run 只保留一条反馈**：再次提交会覆盖，`created_at` 保留，`updated_at` 更新。用户改主意或 Dify 重试，都不会多出记录。
- **和模型的判断分开存**：模型自报的 `answered` 存在 `answers` 表，用户的 `resolved` 存在 `feedback` 表（schema v3）。「模型说答了，用户说没用」正是评测要找的情况。
- **验证**：共 53 个测试，其中 12 个针对 feedback。真实环境：本机数据库启动时升级到 v3；对之前被接受的答案，先提交「已解决」再改成「没解决、引用不对」，库里只留后一条，模型那边的状态仍然是 `answered`；对没有被接受答案的 run 提交，返回 409。

### 已完成：Dify Chatflow（2026-10-05）

- **环境**：本机自托管 Dify 1.17.1（docker compose，放在本仓库外）。只改了三处默认配置：`SECRET_KEY` 换成随机值；网页端口和插件调试端口只绑定 `127.0.0.1`；SSRF 白名单只放行 `host.docker.internal`。生成模型是 DeepSeek `deepseek-v4-flash`，key 由用户自己在 Dify 里填写。
- **交付物**：`workflows/dify-chatflow.yml`，从测试过的 Dify 实例导出，不含密钥。流程、配置方法和 Dify 1.17.1 的几个特殊行为见 `workflows/README.md`。
- **流程要点**：
  - 请求体由代码节点用 `json.dumps` 构造；
  - 模型只引用短编号 `C1`… ，由代码换回真实的 chunk ID；
  - `/api/answers` 拒绝后只修正一次；
  - 用户看到的链接全部由后端生成；
  - 检索出错、没有证据、模型出错、引用校验失败，各自显示不同的提示。
- **为配合真实模型输出改了后端**：
  - `/api/answers` 的链接规则放宽为「被引用片段的规范链接，**或者在被引用片段原文里出现过的 URL**」；
  - URL 正则只匹配合法的 URL 字符；
  - `run_chunks` 增加 `content` 列（schema v4）。
  - 新增 2 个测试，共 55 个。

**2026-10-05 真实集成**（Dify 草稿运行 + 真实检索服务 + DeepSeek）：

| 用例 | 结果 |
| --- | --- |
| Q1 已完成工单再 start | 结论正确（409 `INVALID_STATE_TRANSITION`，在 `_apply`/`next_status` 被拒），引用正确，第一次提交就通过 |
| Q2 不依赖数据库的测试 | **答错了**：回答的是 mineops 的 `make test`，还引用学习手册里的问题，推断出「测试本身不依赖真实 PostgreSQL」。检索没找到 fieldops 的 `pyproject.toml`（demo20 的 d02 也一样），问题本身也没指明是哪个仓库。引用校验通过，但内容不对：这正是校验层管不到、需要回答层评测的情况 |
| Q3 make setup 失败 | 正确，指出了写死的解释器路径，并给出绕过办法 |
| Kafka 消费者组（资料里没有） | `insufficient_evidence`，说明了 mineops 用的是 MQTT |
| 「怎么把服务跑起来？」（有歧义） | 只回答了 mineops，说 fieldops 的片段里没有启动命令（fieldops README 其实有，只是没被检索到）。**没有澄清步骤** |
| 检索服务停掉 | 「连不上检索服务」 |
| 服务在、但没有索引 | `NO_ACTIVE_INDEX` 加 request_id |
| token 不一致 | Dify 报成「被 SSRF 拦截」，流程失败，不展示任何答案（Dify 1.17.1 的行为，NOTES 17） |
| 模型出错（temperature 超出范围） | 「生成模型这次没有返回结果」 |
| 导出的 DSL 作为新应用重新导入 | 没有警告，节点和连线和原来完全一致；在新应用里填好 token 后，Q1 完整答完，第一次提交就通过校验（4.8 秒） |
| Web 应用入口（发布后） | `http://localhost:8090/chat/<code>` 能正常问答；这次第一次提交漏了引用（`CITATION_REQUIRED`），修正一次后通过 |

单次问答耗时 3–7 秒，约 4–5k token（Dify 统计）。没有核实费用。

### 未完成

- **澄清步骤**（spec §6.1 第 1 步）：问题没指明仓库、又可能涉及两个仓库时，应该先问清楚。
- **反馈入口**：Dify 自带的点赞/点踩记在 Dify 自己的库里，没有接到 `/api/feedback`。
- 检索质量：Q2 和有歧义的问题，原因都是检索不到（d02、d18），需要按仓库分别检索、改写查询。
