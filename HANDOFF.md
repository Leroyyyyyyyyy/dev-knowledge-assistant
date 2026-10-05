# HANDOFF

最后更新：2026-10-05

## 1. 当前状态

- **M0 完成，M1 核心完成，M2 进行中**（2026-10-05），详情见 `docs/progress.md`。M2 已完成 `/api/answers`、`/api/feedback`，以及 Dify Chatflow（`workflows/dify-chatflow.yml`，真实环境测过 10 个用例，见 `docs/progress.md`）。共 55 个测试。M2 剩下：澄清步骤、把反馈接进 Dify。
- 检索服务能跑起来：`python -m app.indexing.build` 从固定 commit 构建版本化索引；`uvicorn app.main:app` 提供 `/health`、`/ready`、`/api/retrieve`。25 个自动测试通过，真实 bge-m3 + 两个仓库的端到端已验证（引用链接打开后，行号对应的内容和返回的块逐字一致）。
- 代码结构：`app/indexing/`（快照、构建）、`app/retrieval/`（chunker、encoder、检索服务）、`app/storage/db.py`（SQLite schema）、`app/api/schemas.py`、`app/main.py`、`app/citations.py`；`config/repos.json`；`evals/`；`tests/`。
- 本地运行需要 `.env` 里的 `DKA_SERVICE_TOKEN`（`.env` 已 gitignore，模板见 `.env.example`）。`.claude/launch.json` 里有一个名为 `api` 的启动配置，端口 8077。
- 完整规格在 `docs/spec.md`。

## 2. 原 RAG 项目在哪

原项目分两处，本仓库的上级目录里与它们并列：

| 路径（相对本仓库） | 内容 | 注意 |
| --- | --- | --- |
| `../agent-engineering-log/rag-eval/` | 评测脚本、60 条评测集（中/英）、chunker patch、hybrid / rerank / Ragas 实验 | 历史证据，**原地不动** |
| `../agentic-ai-engineering/01-foundations/06-codebase-navigator/` | 检索代码 `indexer/`、`store/`（共约 460 行），Chroma 索引在 `data/chroma` | 上游跟练仓库，MIT。**不要在里面写新项目代码**；它的 `.env` 有真实凭证，不要碰 |

各自 README：`rag-eval/README.md` 写了实验来历和每个坑，接手前读一遍。

## 3. 已复现的基线（摘要）

60 条，英文查询 @5：纯向量 76%，+rerank 78%（@1 52%），hybrid 64%。中文查询 @5 只有 38%。重建索引前后逐条一致。详细表格和命令见 `docs/baseline.md`。

## 4. 已知陷阱

- **语料没绑 commit**：上游工作区有 21 个改动文件 + 1 个未跟踪文件，都进了索引。M1 必须改成从指定 commit 读。
- **中文查询 + 英文 embedding 模型**：中文召回远低于英文，rerank 对中文 @1 是负效果。
- **navigator 需要 Python 3.13**：`chromadb → onnxruntime` 没有 3.14 的轮子。
- **评测脚本必须在语料之外**：之前放进语料目录后把自己索引了进去，recall 被虚高。新仓库若把自身代码也纳入可索引语料，`evals/` 必须排除。
- **改了切块必须重建索引**：`reindex.py` 先删 collection 再写；chunk id 含起始行号，直接追加会新旧混合。
- `uv run` 会按 `uv.lock` 同步 `.venv`，首次运行会装卸包，属正常。
- **整句机器翻译会改写代码标识符**（`chunk_python` → `cunk_python`）。现在已不走翻译路线；如果以后要对查询做改写，必须保留 ASCII 片段。
- 后台跑实验时 stdout 被重定向到文件会整块缓冲，进度看不到。要加 `PYTHONUNBUFFERED=1`；加 `TQDM_DISABLE=1` 可以关掉模型加载时的进度条刷屏。
- bge-m3 在 MPS 上不能用大批次编码长块：一批会补齐到最长块的长度，显存吃紧后进程直接卡死（NOTES 11）。demo20 已用 `BATCH_SIZE_OVERRIDES` 改成每批 1 块；`demo20_retrieval.py` 写完报告后用 `os._exit(0)` 退出。
- SQLite schema 有版本号（`PRAGMA user_version`，迁移定义在 `app/storage/db.py` 的 `MIGRATIONS` 里）。改表结构**只能加一个新版本**，不要改已有的 SQL，否则已经升级过的数据库不会再执行。
- 在 zsh 里用 `echo "$JSON"` 转发接口响应，会把 JSON 里的 `\n` 变成真的换行，JSON 就坏了。手动测接口用 Python（httpx）。
- **Dify 1.17.1 的几个特殊行为**（详见 `workflows/README.md`、NOTES 15–17）：失败分支会丢掉节点原来的输出；服务停了，Dify 收到的是代理返回的 503；经过代理的 401/403 会被报成「被 SSRF 拦截」。所以 HTTP 节点都不设失败分支。
- Dify 的 `EXPOSE_PLUGIN_DEBUGGING_PORT` 必须是纯端口号：api 容器会把它当作 `PLUGIN_REMOTE_INSTALL_PORT` 使用，写成 `127.0.0.1:5003` 会导致启动失败。要只绑定本机，就用 `docker-compose.override.yaml`，配合 `ports: !override`。
- **chunk 的行号以前是偏的**（NOTES 12）。已在 `chunker.make_chunk` 修好，并有 `tests/test_chunker_lines.py` 覆盖。以后改切块逻辑，必须保证「按行号取出原文 == 块内容」。
- `zh_retrieval.py` 写完报告后**进程不退出**（2026-10-05 实测：15:16 写完报告，之后一直挂着，直到手动停掉）。原因还没查，怀疑是 torch/MPS 或 chromadb 的后台线程。判断跑没跑完要看报告文件里有没有 16 个 arm，不能看进程是否退出。
- Ragas 脚本需要模型 key，本次没跑；规格里写明 Ragas 指标在当前语料上有适配问题，检索层与生成层分开评测。

## 5. 已定的决策（2026-10-05）

- **演示语料改为两个仓库**（spec 原定单仓库，用户决定扩大语料）。两个都是作者自己写的公开仓库，同属采矿运维领域：

  | repo_id（暂定） | 仓库 | 固定 commit | 规模 |
  | --- | --- | --- | --- |
  | `fieldops` | `Leroyyyyyyyyy/fieldops-workorder-bridge` | **待定**，见下方待办 1 | 约 45 个 `.py`、3 个 `.md`，35 个 PR 的历史 |
  | `mineops` | `Leroyyyyyyyyy/mineops` | `85f5da0705718b7d8e6248aa6a2bc12dd9caf09c` | 21 个 `.py`、6 个 `.md`（含 19KB 的中文 `docs/LEARNING_GUIDE.zh-CN.md`），2 个 commit |

  本地 clone 在本仓库的上级目录（`../fieldops-workorder-bridge`、`../mineops`）。双仓库意味着 chunk 元数据、引用 URL、索引版本都要按 `repo_id` 分开，每个仓库绑定各自的 commit。
  - 落选：`commerce-agents`（主体是上游导入，太大）。
- **提问语言：中文。** embedding 暂定 **bge-m3，中文查询直接检索**。旧语料上 @5 38% → 80%；但在 demo20 上，bge-m3 加翻译 @5 更高（63% 对 47%）、@1 更低（5% 对 21%）。n=19 区分不了这两个方案，所以是「暂定」，翻译方案保留作对照。详见 `docs/baseline.md` 的 demo20 一节。

## 6. 待办（按顺序）

1. **fieldops 的 PR 需要用户来合并**。本地分支 `chore/license-and-notes`（在 `../fieldops-workorder-bridge`）已暂存三处改动：新增 MIT `LICENSE`（署名 Liam Ding）、删除 `docs/interview-notes.md`、README 去掉指向该文件的链接并补上 License 一节。commit、push、开 PR、合并这几步被自动权限拦下，**都还没做**，由用户执行。合并后把 fieldops 的固定 commit 填到上表里。注意：删除只影响新 commit，interview-notes 仍留在 git 历史里。
2. **mineops 也没有 LICENSE**，是否补由用户决定。
3. ~~切块扩展名~~ **已完成（2026-10-05）**：`app/retrieval/chunker.py` 新增 `.toml/.ini/.cfg/.conf/.sql/.mako` 和文件名 `Dockerfile/Makefile/.env.example`；`uv.lock`、`LICENSE`、`.gitignore`、`.env` 不索引。fieldops 149 → 157 个 chunk，mineops 106 → 113。上游的扩展名集合保留为 `LEGACY_EXTENSIONS`，`zh_retrieval.py` 显式使用它；已验证 foundations 语料用它切出的 213 个 chunk 和改动前完全一致，基线仍可复现。顺手修了跳过目录的判断（见 NOTES 7）。demo20 上实测：补扩展名只多答对 1 题（d03 的 Makefile），d02 的 `pyproject.toml` 能被检索了，但仍进不了 top-10。
4. ~~建 demo20 评测集~~ **已完成并跑过一轮（2026-10-05）**。评测集冻结在当前两个 commit；`demo20_retrieval.py` 开跑前会核对 commit 和所有锚点，commit 一变就会拒绝运行。
   - mineops 的 `Makefile` `setup` 写死了作者本机的解释器路径（Q3 / d03），是真实缺陷，计划当作 M3 的演示素材，**修之前先确认演示是否还要用它**。
5. **待决定：mineops 的中文学习手册要不要进语料。** 它会吸走中文查询（NOTES 9）。去掉它 bge-m3 @5 47% → 53%，但它对中文用户确实有用（能指到对的文件）。需要作者决定；技术上也可以保留它，同时在检索时限制它在 top-k 里最多占几个位置。
6. **处理三道所有配置都失败的题**（换模型解决不了）：d02（`pyproject.toml` 永远进不了 top-10）、d08（跨仓库题，要按仓库分别检索再合并）、d18（歧义题，回答层应该先澄清）。
7. **扩大评测集**：19 题每题约 5 个百分点，选不出方案。至少扩到 40 题以上，代码定位以外的题型要补，最好能拿到真实用户的问题。
8. **多语言 rerank 单变量实验**：bge-m3 + `bge-reranker-v2-m3`（现有的 ms-marco reranker 只支持英文）。
9. ~~M1 检索服务~~ **核心完成**。剩下的：按仓库分别检索再合并（对应 d08）；进程崩溃后残留的 `building` 记录要能清理（M4 一起做）。
10. **M2 收尾**：澄清步骤（问题没指明仓库时先问清楚）；把 Dify 里的反馈接到 `/api/feedback`。改流程的方法：在 Dify 界面里改好，再导出覆盖 `workflows/dify-chatflow.yml`。不要手写 DSL。
11. **Dify 环境**：在本仓库上级目录 `../dify-1.17.1/docker`（只稀疏检出了 `docker/`）。启动：先开 Docker Desktop，再在那个目录执行 `docker compose up -d`；网页地址 `http://localhost:8090`。它的 `.env` 里改了 `SECRET_KEY`、两个端口绑定、`SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=host.docker.internal`、`APP_WEB_URL` 和 `SERVICE_API_URL`（都设为 `http://localhost:8090`，否则 Dify 显示的链接会少端口）；插件端口通过 `docker-compose.override.yaml` 绑定到本机（直接改变量会让 api 容器起不来，见 NOTES 和 `workflows/README.md`）。Dify 里有两个应用：「研发知识助手」（主应用，已发布，Web 入口是 `http://localhost:8090/chat/<code>`）和「研发知识助手（重新导入验证）」（只用来验证，可以删掉）。Web 应用和 API 只跑**已发布**的版本，改完草稿要记得发布。Dify 的登录会话默认 60 分钟过期，刷新页面就会自动续上。

## 7. 约定

见 `CLAUDE.md`。笔记只写本仓库 `NOTES.md`，交接只写本文件。
