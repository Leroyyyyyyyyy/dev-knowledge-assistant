"""
Materialise a repository exactly as it is at one commit.

The index must never read a working tree: uncommitted edits would end up in the
index while citations point at blob/<commit>/..., and the cited lines would not
match what the reader opens (see docs/baseline.md, known issue 1).
"""

import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPOS_CONFIG = REPO_ROOT / "config" / "repos.json"
SNAPSHOT_DIR = REPO_ROOT / "data" / "snapshots"

# Written last, so a half-extracted snapshot is never mistaken for a complete one.
COMPLETE_MARKER = ".snapshot-complete"


def load_repos(config_path: Path = REPOS_CONFIG) -> dict[str, dict]:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)["repos"]


def export_commit(repo_id: str, repo: dict) -> Path:
    """
    Extract `repo["commit"]` into data/snapshots/<repo_id>@<commit> and return that path.

    Reuses a complete snapshot if one exists. Paths in `repo["exclude"]` are removed
    after extraction, so the index is "this commit minus these paths" and nothing else.
    """
    commit = repo["commit"]
    dest = SNAPSHOT_DIR / f"{repo_id}@{commit}"
    if (dest / COMPLETE_MARKER).exists():
        return dest

    checkout = (REPO_ROOT / repo["checkout"]).resolve()
    if not (checkout / ".git").exists():
        raise FileNotFoundError(f"{repo_id}: no git checkout at {repo['checkout']}; clone {repo['remote']}")

    # Fail with a clear message rather than an empty archive if the commit is not in this clone.
    found = subprocess.run(
        ["git", "-C", str(checkout), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
    )
    if found.returncode != 0:
        raise ValueError(f"{repo_id}: commit {commit} is not in {repo['checkout']}; run git fetch there")

    archive = subprocess.run(
        ["git", "-C", str(checkout), "archive", "--format=tar", commit],
        capture_output=True,
        check=True,
    )

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        # The "data" filter rejects absolute paths, ../ escapes and links that leave dest.
        tar.extractall(dest, filter="data")

    for relative in repo["exclude"]:
        target = dest / relative
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        else:
            raise FileNotFoundError(f"{repo_id}: excluded path {relative} does not exist at {commit}")

    (dest / COMPLETE_MARKER).write_text(commit + "\n", encoding="utf-8")
    return dest
