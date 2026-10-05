"""
Fixtures: two throwaway git repos, a deterministic fake encoder, and an app wired to temp dirs.

The fake encoder hashes words into a small vector, so "a query sharing a rare word
with a chunk ranks that chunk first" holds without downloading a model.
"""

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

import chromadb
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings

TOKEN = "test-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DIMENSIONS = 64


class FakeEncoder:
    def __init__(self, model_name: str = "fake-hash-encoder") -> None:
        self.model_name = model_name

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * DIMENSIONS
        vector[0] = 0.01  # never all-zero, so cosine distance is always defined
        for word in re.findall(r"\w+", text.lower()):
            bucket = int(hashlib.sha1(word.encode("utf-8")).hexdigest(), 16) % (DIMENSIONS - 1) + 1
            vector[bucket] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        normalised = []
        for value in vector:
            normalised.append(value / norm)
        return normalised

    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vectors.append(self._vector(text))
        return vectors

    def encode_query(self, text: str) -> list[float]:
        return self._vector(text)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def make_repo(root: Path, name: str, files: dict[str, str]) -> tuple[Path, str]:
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q")
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo, git(repo, "rev-parse", "HEAD")


@pytest.fixture
def workspace(tmp_path: Path) -> dict:
    alpha, alpha_commit = make_repo(
        tmp_path,
        "alpha",
        {
            "service/orders.py": "def cancel_order(order):\n    raise ZebraTransitionError('cannot cancel')\n",
            "docs/使用 说明.md": "# Setup\n\nRun the quokka installer before anything else.\n",
            "notes/private.md": "personal notes that must never be indexed\n",
            "docs/api.md": "Interactive API docs are served at http://127.0.0.1:8000/docs once it runs.\n",
        },
    )
    # Uncommitted change: must not reach the index, which reads the commit only.
    (alpha / "service" / "orders.py").write_text("def cancel_order(order):\n    uncommittedmarker = 1\n", encoding="utf-8")

    beta, beta_commit = make_repo(
        tmp_path,
        "beta",
        {"ingest/events.py": "def save_event(event):\n    # walrus dedupe on event id\n    return True\n"},
    )

    config_path = tmp_path / "repos.json"
    repos = {
        "alpha": {
            "remote": "https://github.com/example/alpha",
            "checkout": "alpha",
            "commit": alpha_commit,
            "exclude": ["notes/private.md"],
        },
        "beta": {
            "remote": "https://github.com/example/beta",
            "checkout": "beta",
            "commit": beta_commit,
            "exclude": [],
        },
    }
    config_path.write_text(json.dumps({"repos": repos}), encoding="utf-8")

    settings = Settings(
        service_token=TOKEN,
        data_dir=tmp_path / "data",
        repos_config=config_path,
        checkout_base=tmp_path,
        embed_model="fake-hash-encoder",
        top_k_default=5,
        top_k_max=10,
    )
    return {"settings": settings, "repos": repos, "config_path": config_path, "root": tmp_path}


@pytest.fixture
def encoder() -> FakeEncoder:
    return FakeEncoder()


@pytest.fixture
def client(workspace, encoder):
    chroma = chromadb.PersistentClient(path=str(workspace["settings"].chroma_dir))
    app = create_app(workspace["settings"], encoder=encoder, chroma_client=chroma)
    with TestClient(app) as test_client:
        yield test_client
