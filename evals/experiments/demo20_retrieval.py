"""
Retrieval eval on the demo corpus (fieldops + mineops) with the demo20 set.

Ground truth is line ranges at a pinned commit, not code strings: a retrieved
chunk is relevant to an evidence span when repo and path match and the line
ranges overlap. So a re-chunk changes which chunk ids are relevant, but the
labels stay valid.

Arms (one variable changes per comparison):
    minilm-l6 / zh      the old setup, as a floor
    minilm-l6 / mt_keep the best translation route from the foundations60 experiment
    me5-base  / zh      the runner-up multilingual model
    bge-m3    / zh      the chosen setup
    bge-m3    / zh / legacy file set   isolates the effect of indexing config files
    bge-m3    / mt_keep                does translation still hurt bge-m3 on this corpus?
    bge-m3 and minilm / noguide        the same arms without the Chinese learning guide

Usage (from the repo root):
    uv run --group experiments python -m evals.experiments.demo20_retrieval
"""

import json
import os
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

from app.indexing.snapshot import export_commit, load_repos
from app.retrieval.chunker import (
    INDEXABLE_EXTENSIONS,
    INDEXABLE_FILENAMES,
    LEGACY_EXTENSIONS,
    chunk_repository,
)
from app.retrieval.vector_store import VectorStore
from evals.embedders import EMBEDDERS
from evals.scoring import K_VALUES, MAX_K
from evals.translate import MT_MODEL, translate_keep_identifiers

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "evals" / "datasets" / "demo20"
MT_KEEP_CACHE_PATH = DATASET_DIR / "queries_mt_keep_opus.json"
REPORT_DIR = REPO_ROOT / "evals" / "reports"
CHROMA_DIR = REPO_ROOT / "data" / "chroma-experiments"

FILE_SETS = {
    "full": (INDEXABLE_EXTENSIONS, INDEXABLE_FILENAMES),
    "legacy": (LEGACY_EXTENSIONS, set()),
    "noguide": (INDEXABLE_EXTENSIONS, INDEXABLE_FILENAMES),
}

# mineops' Chinese learning guide. With Chinese queries it took the top-1 slot for
# 19/19 queries under MiniLM and 18/19 under e5: query and passage share a language,
# not an answer. "noguide" drops it to measure that effect on its own.
ZH_GUIDE = ("mineops", "docs/LEARNING_GUIDE.zh-CN.md")

# demo20 has chunks up to 2716 bge-m3 tokens (fieldops integration tests). A batch
# is padded to its longest member and attention memory grows with length squared,
# so batches of 8 exhausted MPS memory: indexing took 580s and the next query hung
# in an MPS -> CPU copy. One chunk per batch means no padding; nothing is truncated.
BATCH_SIZE_OVERRIDES = {"bge-m3": 1}

ARMS = [
    ("minilm-l6", "zh", "full"),
    ("minilm-l6", "mt_keep", "full"),
    ("me5-base", "zh", "full"),
    ("bge-m3", "zh", "full"),
    ("bge-m3", "zh", "legacy"),
    ("bge-m3", "mt_keep", "full"),
    ("bge-m3", "zh", "noguide"),
    ("minilm-l6", "mt_keep", "noguide"),
]


def load_dataset() -> dict:
    with open(DATASET_DIR / "eval_set.json", encoding="utf-8") as f:
        return json.load(f)


def check_commits(dataset: dict, repos: dict) -> None:
    """The labels are only valid at the commits they were written against."""
    for repo_id, commit in dataset["repos"].items():
        if repos[repo_id]["commit"] != commit:
            sys.exit(
                f"{repo_id}: 评测集标注于 {commit[:12]}，config/repos.json 现在是 "
                f"{repos[repo_id]['commit'][:12]}。行号已失效，先重新核对标注。"
            )


def validate_spans(dataset: dict, snapshots: dict[str, Path]) -> list[str]:
    """Every span must exist at its commit and contain its anchor text."""
    problems = []
    for item in dataset["queries"]:
        for group in item["evidence"]:
            for span in group:
                path = snapshots[span["repo"]] / span["path"]
                where = f"{item['id']} {span['repo']}:{span['path']}#L{span['start_line']}-L{span['end_line']}"
                if not path.is_file():
                    problems.append(f"{where}: 文件不存在")
                    continue
                lines = path.read_text(encoding="utf-8").split("\n")
                if span["end_line"] > len(lines):
                    problems.append(f"{where}: 文件只有 {len(lines)} 行")
                    continue
                text = "\n".join(lines[span["start_line"] - 1 : span["end_line"]])
                if span["anchor"] not in text:
                    problems.append(f"{where}: 范围内找不到锚点 {span['anchor']!r}")
    return problems


def collect_chunks(repos: dict, snapshots: dict[str, Path], file_set: str) -> list[dict]:
    extensions, filenames = FILE_SETS[file_set]
    chunks = []
    for repo_id, snapshot in snapshots.items():
        for chunk in chunk_repository(snapshot, repo_id, extensions, filenames):
            if file_set == "noguide" and (repo_id, chunk["filepath"]) == ZH_GUIDE:
                continue
            chunk["commit_sha"] = repos[repo_id]["commit"]
            chunks.append(chunk)
    return chunks


def build_index(store: VectorStore, model, model_key: str, spec: dict, collection: str, chunks: list[dict]) -> None:
    """Rebuild one collection from scratch. Same chunk text in every arm; only vectors differ."""
    if store.collection_exists(collection):
        store.client.delete_collection(collection)

    ids = []
    documents = []
    metadatas = []
    embed_inputs = []
    for chunk in chunks:
        ids.append(f"{chunk['repo']}:{chunk['filepath']}:{chunk['start_line']}")
        documents.append(chunk["content"])
        metadatas.append(
            {
                "repo_id": chunk["repo"],
                "commit_sha": chunk["commit_sha"],
                "path": chunk["filepath"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
            }
        )
        embed_inputs.append(spec["passage_prefix"] + chunk["content"])

    embeddings = []
    batch_size = BATCH_SIZE_OVERRIDES.get(model_key, spec["batch_size"])
    for start in range(0, len(embed_inputs), batch_size):
        vectors = model.encode(embed_inputs[start : start + batch_size], show_progress_bar=False)
        embeddings.extend(vectors.tolist())

    store.add_chunks(
        collection_name=collection,
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )


def chunk_hits_span(meta: dict, span: dict) -> bool:
    if meta["repo_id"] != span["repo"] or meta["path"] != span["path"]:
        return False
    return meta["start_line"] <= span["end_line"] and span["start_line"] <= meta["end_line"]


def group_rank(results: list[dict], group: list[dict]) -> int | None:
    """1-based rank of the first chunk that hits any span in the group."""
    for rank, result in enumerate(results, start=1):
        for span in group:
            if chunk_hits_span(result["metadata"], span):
                return rank
    return None


def score_query(item: dict, results: list[dict]) -> dict:
    ranks = []
    for group in item["evidence"]:
        ranks.append(group_rank(results, group))

    scores = {}
    for k in K_VALUES:
        covered = 0
        for rank in ranks:
            if rank is not None and rank <= k:
                covered += 1
        scores[k] = covered / len(ranks)

    top1 = results[0]["metadata"] if results else None
    return {
        "id": item["id"],
        "type": item["type"],
        "metric": "hit" if len(ranks) == 1 else "coverage",
        "query": item["query"],
        "group_ranks": ranks,
        "scores": scores,
        "top1": f"{top1['repo_id']}:{top1['path']}#L{top1['start_line']}-L{top1['end_line']}" if top1 else None,
        "top1_distance": results[0]["distance"] if results else None,
    }


def mean_at(rows: list[dict], k: int) -> float:
    if not rows:
        return 0.0
    total = 0.0
    for row in rows:
        total += row["scores"][k]
    return total / len(rows)


def arm_label(arm: dict) -> str:
    return f"{arm['model_key']} / {arm['query_mode']} / {arm['file_set']}"


def print_summary(arms: list[dict], types: list[str]) -> None:
    print("\n" + "=" * 92)
    n = len(arms[0]["rows"])
    print(f"整体 (n={n}，有证据的题；单组按 hit，多组按 coverage，混合平均)")
    print("=" * 92)
    header = f"  {'arm':<34}"
    for k in K_VALUES:
        header += f"   @{k:<3}"
    print(header)
    for arm in arms:
        line = f"  {arm_label(arm):<34}"
        for k in K_VALUES:
            line += f"   {mean_at(arm['rows'], k):>4.0%}"
        print(line)

    print("\n" + "=" * 92)
    print("按题型 @5 (括号内为题数)")
    print("=" * 92)
    present = []
    for question_type in types:
        count = 0
        for row in arms[0]["rows"]:
            if row["type"] == question_type:
                count += 1
        if count:
            present.append((question_type, count))
    header = f"  {'arm':<34}"
    for question_type, count in present:
        header += f" {question_type[:13] + f'({count})':>17}"
    print(header)
    for arm in arms:
        line = f"  {arm_label(arm):<34}"
        for question_type, _count in present:
            subset = []
            for row in arm["rows"]:
                if row["type"] == question_type:
                    subset.append(row)
            line += f" {mean_at(subset, 5):>17.0%}"
        print(line)

    print("\n" + "=" * 92)
    print("逐题：每组证据的首次命中排名 (- = top-10 未命中)")
    print("=" * 92)
    header = f"  {'id':<4} {'type':<15}"
    for arm in arms:
        header += f" {arm['model_key'][:9] + '/' + arm['query_mode'][:2] + '/' + arm['file_set'][:3]:>16}"
    print(header)
    for index, row in enumerate(arms[0]["rows"]):
        line = f"  {row['id']:<4} {row['type']:<15}"
        for arm in arms:
            parts = []
            for rank in arm["rows"][index]["group_ranks"]:
                parts.append(str(rank) if rank is not None else "-")
            line += f" {','.join(parts):>16}"
        print(line)


def main() -> None:
    from sentence_transformers import SentenceTransformer

    dataset = load_dataset()
    repos = load_repos()
    check_commits(dataset, repos)

    snapshots = {}
    for repo_id, repo in repos.items():
        snapshots[repo_id] = export_commit(repo_id, repo)

    problems = validate_spans(dataset, snapshots)
    if problems:
        print(f"评测集有 {len(problems)} 处问题:")
        for problem in problems:
            print("  -", problem)
        sys.exit(1)

    scored = []
    skipped = []
    for item in dataset["queries"]:
        if item["evidence"]:
            scored.append(item)
        else:
            skipped.append(item["id"])
    print(f"{len(dataset['queries'])} 题，全部证据范围和锚点核对通过；{len(skipped)} 题无证据不计检索分: {skipped}")

    query_texts = {"zh": {}}
    for item in dataset["queries"]:
        query_texts["zh"][item["id"]] = item["query"]
    query_texts["mt_keep"] = translate_keep_identifiers(dataset["queries"], MT_KEEP_CACHE_PATH)

    store = VectorStore(persist_dir=str(CHROMA_DIR))
    models = {}
    indexed = {}
    arms = []
    for model_key, query_mode, file_set in ARMS:
        spec = EMBEDDERS[model_key]
        if model_key not in models:
            print(f"\n加载 {spec['model']} ...")
            models[model_key] = SentenceTransformer(spec["model"])
        model = models[model_key]

        collection = f"demo-{model_key}-{file_set}"
        if collection not in indexed:
            chunks = collect_chunks(repos, snapshots, file_set)
            per_repo = Counter()
            for chunk in chunks:
                per_repo[chunk["repo"]] += 1
            started = time.perf_counter()
            build_index(store, model, model_key, spec, collection, chunks)
            indexed[collection] = {"chunks": len(chunks), "seconds": round(time.perf_counter() - started, 1)}
            print(f"  {collection}: {len(chunks)} 个 chunk {dict(per_repo)}，索引 {indexed[collection]['seconds']}s")

        rows = []
        for item in scored:
            query = spec["query_prefix"] + query_texts[query_mode][item["id"]]
            vector = model.encode(query).tolist()
            results = store.search(query_embedding=vector, collection_name=collection, n_results=MAX_K)
            rows.append(score_query(item, results))

        arm = {
            "model_key": model_key,
            "model": spec["model"],
            "query_mode": query_mode,
            "file_set": file_set,
            "index": indexed[collection],
            "rows": rows,
        }
        arms.append(arm)
        print(f"  {arm_label(arm)}: @5 = {mean_at(rows, 5):.0%}")

    print_summary(arms, dataset["types"])

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"demo20_retrieval_{date.today().isoformat()}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(
            {"dataset": f"{dataset['name']} {dataset['version']}", "repos": dataset["repos"], "mt_model": MT_MODEL, "arms": arms},
            f,
            ensure_ascii=False,
            indent=1,
        )
    print(f"\n逐条结果已写入 {report_path.relative_to(REPO_ROOT)}")
    sys.stdout.flush()
    # The foundations60 run hung after writing its report (HANDOFF, known traps).
    # Everything is on disk by now, so leave without waiting for background threads.
    os._exit(0)


if __name__ == "__main__":
    main()
