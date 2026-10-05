"""
Chinese-query retrieval experiment on the foundations60 set.

Question: the corpus is English code and docs, users will ask in Chinese, and the
baseline embedding (all-MiniLM-L6-v2) is English-only. Which fix works better?

    A. swap in a multilingual embedding model (query stays Chinese)
    B. translate the query to English first, keep the original model

Every arm uses the same corpus, the same chunks, the same Chroma settings and the
same scoring. Per arm only one thing changes: the embedding model, or the query text.

Query modes:
    zh  original Chinese query
    en  hand-written English version (the translation ceiling: a perfect translator)
    mt  machine translation of zh with a local opus-mt-zh-en model (a real translator)
    mt_keep  same translator, but only the Chinese runs are translated; code identifiers are kept

Usage (from the repo root):
    uv run --group experiments python -m evals.experiments.zh_retrieval
    MODELS=minilm-l6,bge-m3 uv run --group experiments python -m evals.experiments.zh_retrieval

FOUNDATIONS_CORPUS points at the 01-foundations directory of agentic-ai-engineering;
it defaults to a sibling checkout of this repository.
"""

import json
import os
import sys
import time
from datetime import date
from pathlib import Path

from app.retrieval.chunker import LEGACY_EXTENSIONS, chunk_repository, collect_files
from app.retrieval.vector_store import VectorStore
from evals.scoring import K_VALUES, MAX_K, expected_strings, first_hit_rank
from evals.scoring import found_count, mean_score, metric_of, score_at, validate_ground_truth
from evals.embedders import EMBEDDERS
from evals.translate import MT_MODEL, translate_keep_identifiers

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "evals" / "datasets" / "foundations60"
MT_CACHE_PATH = DATASET_DIR / "queries_mt_opus.json"
MT_KEEP_CACHE_PATH = DATASET_DIR / "queries_mt_keep_opus.json"
REPORT_DIR = REPO_ROOT / "evals" / "reports"
CHROMA_DIR = REPO_ROOT / "data" / "chroma-experiments"

DEFAULT_CORPUS = REPO_ROOT.parent / "agentic-ai-engineering" / "01-foundations"
CORPUS = Path(os.environ.get("FOUNDATIONS_CORPUS", DEFAULT_CORPUS)).resolve()

QUERY_MODES = ["zh", "en", "mt", "mt_keep"]


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_corpus_text() -> str:
    """All indexable files concatenated, for the ground-truth self-check."""
    parts = []
    for path in collect_files(CORPUS, LEGACY_EXTENSIONS, set()):
        parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def translate_queries(queries: list[dict]) -> dict[str, str]:
    """
    Machine-translate every Chinese query once and cache the result on disk.

    The cache is committed with the dataset so the mt arm is reproducible and
    every translation can be read and judged by a person.
    """
    if MT_CACHE_PATH.exists():
        cached = load_json(MT_CACHE_PATH)
        if cached.get("model") == MT_MODEL and len(cached["queries"]) == len(queries):
            return cached["queries"]

    from transformers import MarianMTModel, MarianTokenizer

    print(f"翻译 {len(queries)} 条查询 ({MT_MODEL}) ...")
    tokenizer = MarianTokenizer.from_pretrained(MT_MODEL)
    model = MarianMTModel.from_pretrained(MT_MODEL)

    translated = {}
    for item in queries:
        batch = tokenizer([item["query"]], return_tensors="pt")
        output = model.generate(**batch, max_new_tokens=128, num_beams=4)
        translated[item["id"]] = tokenizer.decode(output[0], skip_special_tokens=True)

    with open(MT_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "note": "Machine translation of eval_set.json queries, zh -> en. Generated, not hand-edited.",
                "model": MT_MODEL,
                "queries": translated,
            },
            f,
            ensure_ascii=False,
            indent=1,
        )
    return translated


def build_index(store: VectorStore, model, spec: dict, collection: str, chunks: list[dict]) -> None:
    """
    Rebuild one collection from scratch for one embedding model.

    Same ids, documents and metadata as the original index_chunks(); only the
    vectors differ. The passage prefix goes into the embedding input, never into
    the stored document, so scoring sees identical chunk text in every arm.
    """
    if store.collection_exists(collection):
        store.client.delete_collection(collection)

    ids = []
    documents = []
    metadatas = []
    embed_inputs = []
    for chunk in chunks:
        ids.append(f"{collection}:{chunk['filepath']}:{chunk['start_line']}")
        documents.append(chunk["content"])
        metadatas.append(
            {
                "filepath": chunk["filepath"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "repo": chunk["repo"],
            }
        )
        embed_inputs.append(spec["passage_prefix"] + chunk["content"])

    embeddings = []
    batch_size = spec["batch_size"]
    for start in range(0, len(embed_inputs), batch_size):
        batch = embed_inputs[start : start + batch_size]
        vectors = model.encode(batch, show_progress_bar=False)
        embeddings.extend(vectors.tolist())

    store.add_chunks(
        collection_name=collection,
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )


def run_arm(store, model, spec, collection, queries, query_texts) -> list[dict]:
    """Score every query once for one (model, query mode) pair."""
    rows = []
    for item in queries:
        query = query_texts[item["id"]]
        vector = model.encode(spec["query_prefix"] + query).tolist()
        results = store.search(query_embedding=vector, collection_name=collection, n_results=MAX_K)

        expected = expected_strings(item)
        scores = {}
        for k in K_VALUES:
            scores[k] = score_at(item, results, k)

        rows.append(
            {
                "id": item["id"],
                "category": item["category"],
                "metric": metric_of(item),
                "query": query,
                "scores": scores,
                "rank": first_hit_rank(results, expected),
                "found_at_10": found_count(results, expected, MAX_K),
                "total": len(expected),
            }
        )
    return rows


def rows_in_category(rows: list[dict], category: str) -> list[dict]:
    subset = []
    for row in rows:
        if row["category"] == category:
            subset.append(row)
    return subset


def print_summary(arms: list[dict], categories: list[str]) -> None:
    print("\n" + "=" * 96)
    print("整体 (hit 与 coverage 混合平均, n=60)")
    print("=" * 96)
    header = f"  {'model':<18} {'query':<7} {'max_len':>7}"
    for k in K_VALUES:
        header += f"   @{k:<3}"
    print(header)
    for arm in arms:
        line = f"  {arm['model_key']:<18} {arm['query_mode']:<7} {arm['max_seq_length']:>7}"
        for k in K_VALUES:
            line += f"   {mean_score(arm['rows'], k):>4.0%}"
        print(line)

    print("\n" + "=" * 96)
    print("按类别 @5")
    print("=" * 96)
    header = f"  {'model':<18} {'query':<7}"
    for category in categories:
        header += f"  {category[:16]:>16}"
    print(header)
    for arm in arms:
        line = f"  {arm['model_key']:<18} {arm['query_mode']:<7}"
        for category in categories:
            line += f"  {mean_score(rows_in_category(arm['rows'], category), 5):>16.0%}"
        print(line)


def main() -> None:
    from sentence_transformers import SentenceTransformer

    if not CORPUS.is_dir():
        sys.exit(f"语料目录不存在: {CORPUS}\n用 FOUNDATIONS_CORPUS=<path>/01-foundations 指定")

    eval_set = load_json(DATASET_DIR / "eval_set.json")
    queries = eval_set["queries"]
    categories = list(eval_set["categories"])

    problems = validate_ground_truth(queries, read_corpus_text())
    if problems:
        print(f"评测集有 {len(problems)} 处问题:")
        for problem in problems:
            print("  -", problem)
        sys.exit(1)
    print(f"{len(queries)} 条 ground truth 全部在语料中找到")

    # The legacy file set keeps this experiment comparable with docs/baseline.md.
    chunks = chunk_repository(CORPUS, CORPUS.name, LEGACY_EXTENSIONS, set())
    print(f"语料 {CORPUS.name}: {len(chunks)} 个 chunk")

    query_texts = {"zh": {}, "en": load_json(DATASET_DIR / "queries_en.json")["queries"]}
    for item in queries:
        query_texts["zh"][item["id"]] = item["query"]
    query_texts["mt"] = translate_queries(queries)
    query_texts["mt_keep"] = translate_keep_identifiers(queries, MT_KEEP_CACHE_PATH)

    model_keys = list(EMBEDDERS)
    if os.environ.get("MODELS"):
        model_keys = os.environ["MODELS"].split(",")

    store = VectorStore(persist_dir=str(CHROMA_DIR))
    arms = []
    for model_key in model_keys:
        spec = EMBEDDERS[model_key]
        print(f"\n加载 {spec['model']} ...")
        model = SentenceTransformer(spec["model"])
        collection = f"f01-{model_key}"

        started = time.perf_counter()
        build_index(store, model, spec, collection, chunks)
        index_seconds = time.perf_counter() - started
        print(f"  索引 {len(chunks)} 个 chunk 用时 {index_seconds:.1f}s, max_seq_length={model.max_seq_length}")

        for query_mode in QUERY_MODES:
            rows = run_arm(store, model, spec, collection, queries, query_texts[query_mode])
            arms.append(
                {
                    "model_key": model_key,
                    "model": spec["model"],
                    "max_seq_length": model.max_seq_length,
                    "query_mode": query_mode,
                    "index_seconds": round(index_seconds, 1),
                    "rows": rows,
                }
            )
            print(f"  {query_mode}: @5 = {mean_score(rows, 5):.0%}")

    print_summary(arms, categories)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"zh_retrieval_{date.today().isoformat()}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(
            {"corpus": CORPUS.name, "chunks": len(chunks), "mt_model": MT_MODEL, "arms": arms},
            f,
            ensure_ascii=False,
            indent=1,
        )
    print(f"\n逐条结果已写入 {report_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
