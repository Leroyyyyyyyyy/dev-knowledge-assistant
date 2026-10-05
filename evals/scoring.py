"""
Retrieval scoring for string-labelled eval sets (foundations60 format).

Same rules as the original rag-eval/run_eval.py, so numbers stay comparable:

    expect_any  -> hit@k       备选写法, top-k 里命中任一就算答上
    expect_all  -> coverage@k  答案分散在多处, 分数 = 命中数 / 应命中数

A chunk "contains" an expected string by plain substring match on its content.
"""

K_VALUES = [1, 3, 5, 10]
MAX_K = max(K_VALUES)


def expected_strings(item: dict) -> list[str]:
    """Every ground-truth string for a query, whichever field it declares."""
    if "expect_all" in item:
        return item["expect_all"]
    return item["expect_any"]


def metric_of(item: dict) -> str:
    """Which metric this query is scored with."""
    if "expect_all" in item:
        return "coverage"
    return "hit"


def first_hit_rank(results: list[dict], expected: list[str]) -> int | None:
    """1-based rank of the first chunk containing any expected string."""
    for rank, result in enumerate(results, start=1):
        for needle in expected:
            if needle in result["content"]:
                return rank
    return None


def found_count(results: list[dict], expected: list[str], k: int) -> int:
    """How many of the expected strings appear anywhere in the top-k chunks."""
    found = 0
    for needle in expected:
        for result in results[:k]:
            if needle in result["content"]:
                found += 1
                break
    return found


def score_at(item: dict, results: list[dict], k: int) -> float:
    """Score one query at k with the metric its schema asks for."""
    expected = expected_strings(item)
    if metric_of(item) == "coverage":
        return found_count(results, expected, k) / len(expected)

    rank = first_hit_rank(results, expected)
    if rank is not None and rank <= k:
        return 1.0
    return 0.0


def mean_score(rows: list[dict], k: int) -> float:
    """Average score at k over the given rows."""
    if not rows:
        return 0.0
    total = 0.0
    for row in rows:
        total += row["scores"][k]
    return total / len(rows)


def validate_ground_truth(queries: list[dict], corpus_text: str) -> list[str]:
    """Every expected string must exist in the corpus, or the numbers mean nothing."""
    problems = []
    for item in queries:
        if ("expect_any" in item) == ("expect_all" in item):
            problems.append(f"{item['id']}: 必须且只能有 expect_any 或 expect_all 其中一个")
            continue
        for expected in expected_strings(item):
            if expected not in corpus_text:
                problems.append(f"{item['id']}: 语料里找不到 {expected!r}")
    return problems
