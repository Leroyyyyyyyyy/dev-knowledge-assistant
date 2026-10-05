# 需求：三个真实问题

2026-10-05。对应 spec §2.1 的三类任务，每类一题。

**来源说明**：这三题由开发助手读两个演示仓库的代码后起草，经作者认可；答案都在代码里核对过，Q2 实际跑过。它们**不是**真实试用者提出的问题。有试用者之后，按试用记录补充或替换（spec §11.2）。

语料版本：`fieldops` 暂用 `1b5fe9c`（LICENSE 那个 PR 合并前的 main；这次引用的文件不受那个 PR 影响），`mineops` 用 `85f5da0`。

---

## Q1 找实现（fieldops）

> 对一张已经完成的工单再调用 start，会在哪一步被拒绝？返回什么？

**原有处理方式**：在仓库里搜 `409` 或 `start`，从路由 `app/api/work_orders.py` 一路读到领域模块。要跨两个文件才能看完整。

**需要的结果**：

- 规则在哪：`app/domain/work_order_status.py` L83–97 的 `TRANSITIONS` 表（START 只允许从 ASSIGNED 出发），L102–117 的 `InvalidTransition` 和 `next_status`。
- 怎么变成 HTTP 响应：`app/api/work_orders.py` L115–135 的 `_apply` 捕获 `InvalidTransition`，抛出 409，`code` 为 `INVALID_STATE_TRANSITION`，`message` 为 `Cannot START a work order in status COMPLETED.`。
- 附带结论：异常在修改 `status` 和 `version` 之前抛出，也就到不了 `_record`，所以被拒绝的命令不会留下审计事件。
- 引用指向上面两个文件的具体行号。

**不可接受的错误**：

- 说成数据库约束或 CHECK 拦下的。
- 说成返回 400 或 422。
- 编造一个通用的 `PATCH status` 接口。README 明确说没有这个接口。
- 只引用其中一个文件，却给出完整结论。

## Q2 查操作（fieldops）

> 本机没装 Docker，只想先跑不依赖数据库的测试，命令是什么？

**原有处理方式**：README L38–40 只写了 `uv run pytest`，并注明 93 个测试需要数据库。不起数据库直接跑会失败，要自己去 `pyproject.toml` 和测试文件里找出路。

**需要的结果**：

- 命令：`uv sync`，然后 `uv run pytest -m "not integration"`。2026-10-05 实测结果为 `30 passed, 63 deselected`。
- 依据：`pyproject.toml` L42–50 定义了 `integration` marker（「需要真实 PostgreSQL 的测试」）；`tests/integration/` 下的 7 个测试文件都声明了 `pytestmark = pytest.mark.integration`。
- 说明 README 没有写这条命令，它是从配置推出来的。

**不可接受的错误**：

- 只给 `uv run pytest`。没有数据库会失败。
- 编造不存在的参数或环境变量。
- 声称 README 写了这条命令。

**依赖**：答案的关键证据在 `pyproject.toml`。这个文件在 chunker 补扩展名之前不会被索引（见 NOTES 8），可以作为补扩展名的回归题。

## Q3 转问题单（mineops）

> 按 README 在一台新机器上执行 `make setup`，第一步就失败，提示找不到 Python 解释器。怎么办？

**原有处理方式**：自己读 Makefile，或者问作者。

**需要的结果**：

- 定位原因：`Makefile` L5–8 的 `setup` 目标第一行用**写死的作者本机解释器绝对路径**创建 venv，没有用同文件 L1 定义的 `PYTHON` 变量，也没有用系统的 `python3`。README L40–45 让用户直接执行 `make setup`；`pyproject.toml` 只要求 Python ≥ 3.11。这个路径在作者本机存在，所以作者自己永远看不到这个问题。
- 临时绕过：手动执行 `python3 -m venv .venv`，再执行 `.venv/bin/python -m pip install -e ".[dev]"`。这两条就是 setup 目标剩下的步骤，只是把解释器换成系统的 Python ≥ 3.11。
- 这是仓库本身的缺陷，回答之后引导用户整理问题单。草稿应包含：标题、复现步骤（`git clone` → `make setup`）、相关代码引用 `Makefile#L5-L8 @ 85f5da0`、已尝试的绕过方法、环境信息。

**不可接受的错误**：

- 编造报错原文。报错只能写用户实际提供的；用户没给，就标为「未提供」。
- 建议用户在自己机器上创建那个绝对路径。
- 不经确认页就自动提交 Issue。
- 把作者本机的绝对路径抄进草稿正文。引用代码行就够了，不要在正文里重复私人路径。

**演示提示**：这个缺陷目前真实存在。如果想在 M3 演示里用它，**先不要修**；真实 Issue 只发到作者指定的测试仓库（spec §7.1）。
