"""
Search with the Chinese question and an English translation, then fuse, on demo20.

d02 showed the gap: the user asks for tests that "do not depend on a database",
the file says "integration: tests that require a real PostgreSQL database".
In fieldops alone that chunk ranks 18th for the Chinese question and 2nd for
its English translation.

Arms (each scored with its own top_k per cut-off):
  zh            the Chinese question (what the service does today)
  en            the English translation only
  rrf           both, fused with reciprocal rank fusion
  *_scoped      the same, restricted to the repositories the evidence is in
                (what the service gets once the repository is settled)

Translations: a file next to the dataset (see each file's note).
Usage: uv run python -m evals.experiments.demo20_fusion [translations.json] [dataset]
  dataset defaults to demo20 (the development set); heldout24 is the test set.
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

import chromadb

from app.indexing.build import collection_name
from app.retrieval.encoder import SentenceTransformerEncoder
from app.retrieval.service import FUSION_DEPTH, RRF_K, fuse, search
from app.settings import Settings
from app.storage.db import active_version, connect
from evals.experiments.demo20_retrieval import mean_at, score_query
from evals.experiments.demo20_scope import labeled_repos, take_turns
from evals.scoring import K_VALUES

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRANSLATIONS = "queries_en_llm.json"
ARMS = ["zh", "en", "rrf", "zh_scoped", "en_scoped", "rrf_scoped"]

def ranked(collection, embedding: list[float], repos: list[str] | None, top_k: int) -> list[dict]:
    if repos is None:
        return search(collection, embedding, None, top_k)
    if len(repos) == 1:
        return search(collection, embedding, repos[0], top_k)
    return take_turns(collection, embedding, repos, top_k)


def run_arm(arm: str, collection, zh_embedding, en_embedding, repos, top_k: int) -> list[dict]:
    scope = repos if arm.endswith("_scoped") else None
    mode = arm.removesuffix("_scoped")
    if mode == "zh":
        return ranked(collection, zh_embedding, scope, top_k)
    if mode == "en":
        return ranked(collection, en_embedding, scope, top_k)
    if mode == "rrf":
        zh_hits = ranked(collection, zh_embedding, scope, FUSION_DEPTH)
        en_hits = ranked(collection, en_embedding, scope, FUSION_DEPTH)
        return fuse([zh_hits, en_hits], top_k)
    raise ValueError(arm)


def main() -> None:
    translations_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRANSLATIONS
    dataset_name = sys.argv[2] if len(sys.argv) > 2 else "demo20"
    dataset_dir = REPO_ROOT / "evals" / "datasets" / dataset_name
    translations_path = dataset_dir / translations_name
    # Keep the first report's name for the default file, so earlier results stay where they were.
    if dataset_name == "demo20" and translations_name == DEFAULT_TRANSLATIONS:
        report_path = REPO_ROOT / "evals" / "reports" / "demo20_fusion.json"
    else:
        translator = translations_path.stem.removeprefix("queries_en_")
        report_path = REPO_ROOT / "evals" / "reports" / f"{dataset_name}_fusion_{translator}.json"
    dataset = json.loads((dataset_dir / "eval_set.json").read_text(encoding="utf-8"))

    settings = Settings()
    connection = connect(settings.db_path)
    version_row = active_version(connection)
    connection.close()
    manifest = json.loads(version_row["manifest"])

    for repo_id, frozen_commit in dataset["repos"].items():
        if manifest[repo_id]["commit"] != frozen_commit:
            raise SystemExit(f"active index has {repo_id}@{manifest[repo_id]['commit']}, demo20 is frozen at {frozen_commit}")
    translations = json.loads(translations_path.read_text(encoding="utf-8"))["queries"]

    collection = chromadb.PersistentClient(path=str(settings.chroma_dir)).get_collection(collection_name(version_row["version"]))
    encoder = SentenceTransformerEncoder(version_row["embed_model"], settings.embed_batch_size)

    scored = [item for item in dataset["queries"] if item["evidence"]]
    rows = {arm: [] for arm in ARMS}
    for item in scored:
        zh_embedding = encoder.encode_query(item["query"])
        en_embedding = encoder.encode_query(translations[item["id"]])
        repos = labeled_repos(item)
        for arm in ARMS:
            per_k = {}
            for k in K_VALUES:
                per_k[k] = score_query(item, run_arm(arm, collection, zh_embedding, en_embedding, repos, k))
            row = dict(per_k[max(K_VALUES)])
            row["scores"] = {}
            for k in K_VALUES:
                row["scores"][k] = per_k[k]["scores"][k]
            rows[arm].append(row)

    print(f"dataset {dataset_name}, translations {translations_name}")
    print(f"index {version_row['version']} ({version_row['embed_model']}), {len(scored)} scored queries\n")
    header = "arm".ljust(12)
    for k in K_VALUES:
        header += f"  @{k:<4}"
    print(header)
    for arm in ARMS:
        line = arm.ljust(12)
        for k in K_VALUES:
            line += f"  {mean_at(rows[arm], k):.0%}".ljust(7)
        print(line)

    print("\nper query @5: " + " / ".join(ARMS))
    for index, item in enumerate(scored):
        cells = []
        for arm in ARMS:
            cells.append(f"{rows[arm][index]['scores'][5]:.2f}")
        print(f"  {item['id']} {item['type']:<16} {'  '.join(cells)}")

    report_path.write_text(
        json.dumps(
            {
                "date": date.today().isoformat(),
                "index_version": version_row["version"],
                "embed_model": version_row["embed_model"],
                "dataset_version": dataset["version"],
                "translations": translations_path.name,
                "fusion": {"depth": FUSION_DEPTH, "rrf_k": RRF_K},
                "arms": {arm: {"mean": {k: mean_at(rows[arm], k) for k in K_VALUES}, "queries": rows[arm]} for arm in ARMS},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nreport: {report_path.relative_to(REPO_ROOT)}")
    os._exit(0)


if __name__ == "__main__":
    main()
