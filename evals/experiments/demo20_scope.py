"""
How the chunks of several repositories are combined, measured on demo20.

Runs against the service's active index (the same chunks and model the service
answers from), with the service's own search function. Nothing is written to
the service database.

Arms, all with the same query embedding:
  global         one ranking over every repository (what the service does)
  turns          each repository searched on its own, then taken in turns
  turns_sorted   the same chunks as 'turns', reordered by distance
  labeled_repo   only the repositories the evidence is in (an upper bound: it
                 assumes the repository is known, e.g. after clarification)

Usage: uv run python -m evals.experiments.demo20_scope
"""

import json
import os
from datetime import date
from pathlib import Path

import chromadb

from app.indexing.build import collection_name
from app.retrieval.encoder import SentenceTransformerEncoder
from app.retrieval.service import search
from app.settings import Settings
from app.storage.db import active_version, connect
from evals.experiments.demo20_retrieval import load_dataset, mean_at, score_query
from evals.scoring import K_VALUES

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = REPO_ROOT / "evals" / "reports" / "demo20_scope.json"
ARMS = ["global", "turns", "turns_sorted", "labeled_repo"]


def take_turns(collection, embedding: list[float], repo_ids: list[str], top_k: int) -> list[dict]:
    """Search each repository on its own; take the best of each, then the second best of each, ..."""
    per_repo = []
    for repo_id in repo_ids:
        per_repo.append(search(collection, embedding, repo_id, top_k))
    chosen = []
    for position in range(top_k):
        for hits in per_repo:
            if position < len(hits) and len(chosen) < top_k:
                chosen.append(hits[position])
    return chosen


def labeled_repos(item: dict) -> list[str]:
    repos = []
    for group in item["evidence"]:
        for span in group:
            if span["repo"] not in repos:
                repos.append(span["repo"])
    return sorted(repos)


def run_arm(arm: str, collection, embedding: list[float], item: dict, all_repos: list[str], top_k: int) -> list[dict]:
    if arm == "global":
        return search(collection, embedding, None, top_k)
    if arm == "turns":
        return take_turns(collection, embedding, all_repos, top_k)
    if arm == "turns_sorted":
        hits = take_turns(collection, embedding, all_repos, top_k)
        return sorted(hits, key=lambda hit: hit["distance"])
    if arm == "labeled_repo":
        repos = labeled_repos(item)
        if len(repos) == 1:
            return search(collection, embedding, repos[0], top_k)
        return take_turns(collection, embedding, repos, top_k)
    raise ValueError(arm)


def main() -> None:
    settings = Settings()
    connection = connect(settings.db_path)
    version_row = active_version(connection)
    connection.close()
    manifest = json.loads(version_row["manifest"])
    all_repos = sorted(manifest)

    dataset = load_dataset()
    for repo_id, frozen_commit in dataset["repos"].items():
        if manifest[repo_id]["commit"] != frozen_commit:
            raise SystemExit(f"active index has {repo_id}@{manifest[repo_id]['commit']}, demo20 is frozen at {frozen_commit}")

    collection = chromadb.PersistentClient(path=str(settings.chroma_dir)).get_collection(collection_name(version_row["version"]))
    encoder = SentenceTransformerEncoder(version_row["embed_model"], settings.embed_batch_size)

    scored = [item for item in dataset["queries"] if item["evidence"]]
    rows = {arm: [] for arm in ARMS}
    for item in scored:
        embedding = encoder.encode_query(item["query"])
        for arm in ARMS:
            # Score each cut-off with its own top_k: a sorted merge of 5 is not
            # the first 5 of a sorted merge of 10.
            per_k = {}
            for k in K_VALUES:
                per_k[k] = score_query(item, run_arm(arm, collection, embedding, item, all_repos, k))
            row = dict(per_k[max(K_VALUES)])
            row["scores"] = {}
            for k in K_VALUES:
                row["scores"][k] = per_k[k]["scores"][k]
            rows[arm].append(row)

    print(f"index {version_row['version']} ({version_row['embed_model']}), {len(scored)} scored queries\n")
    header = "arm".ljust(14)
    for k in K_VALUES:
        header += f"  @{k:<4}"
    print(header)
    for arm in ARMS:
        line = arm.ljust(14)
        for k in K_VALUES:
            line += f"  {mean_at(rows[arm], k):.0%}".ljust(7)
        print(line)

    print("\nper query @5 (global → turns → turns_sorted → labeled_repo)")
    for index, item in enumerate(scored):
        cells = []
        for arm in ARMS:
            cells.append(f"{rows[arm][index]['scores'][5]:.2f}")
        changed = "" if len(set(cells)) == 1 else "  *"
        print(f"  {item['id']} {item['type']:<16} {' → '.join(cells)}{changed}")

    REPORT_PATH.write_text(
        json.dumps(
            {
                "date": date.today().isoformat(),
                "index_version": version_row["version"],
                "embed_model": version_row["embed_model"],
                "dataset_version": dataset["version"],
                "arms": {arm: {"mean": {k: mean_at(rows[arm], k) for k in K_VALUES}, "queries": rows[arm]} for arm in ARMS},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nreport: {REPORT_PATH.relative_to(REPO_ROOT)}")
    # torch / chromadb background threads can keep the process alive (HANDOFF).
    os._exit(0)


if __name__ == "__main__":
    main()
