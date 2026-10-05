"""
Code Chunker

Splits source code files into chunks suitable for embedding and semantic search.
Uses simple heuristics: Python files split on class/function definitions,
other files split by line count with overlap.
"""

from pathlib import Path
from typing import Any

import logging

logger = logging.getLogger(__name__)

# The upstream extension set. Kept unchanged so the foundations60 regression
# (docs/baseline.md) can still be reproduced chunk for chunk.
LEGACY_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".php",
    ".c",
    ".cpp",
    ".h",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
}

# How-to answers ("how do I start it / run the tests / migrate") mostly live in
# config and build files, which the upstream set skipped entirely.
INDEXABLE_EXTENSIONS = LEGACY_EXTENSIONS | {
    ".toml",  # pyproject.toml: dependencies, tool config, scripts
    ".ini",  # alembic.ini, pytest.ini
    ".cfg",  # setup.cfg
    ".conf",  # service config, e.g. mosquitto.conf
    ".sql",  # schema files
    ".mako",  # Alembic migration template
}

# Files that matter but have no suffix, or whose suffix says nothing.
# .env is deliberately absent: it holds real credentials. .env.example holds placeholders.
INDEXABLE_FILENAMES = {
    "Dockerfile",
    "Makefile",
    ".env.example",
}

# Directories to skip
SKIP_DIRS = {
    "node_modules",
    "venv",
    ".venv",
    ".git",
    "__pycache__",
    "dist",
    "build",
    ".next",
    "vendor",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
    "egg-info",
    # Never index the navigator's own artifacts: cloned third-party repos and
    # the ChromaDB store itself. Indexing a local path used to sweep these in,
    # and they drowned out the real corpus (73% of chunks came from repos/).
    "repos",
    "data",
}

# Max lines per chunk for non-Python files
CHUNK_SIZE = 50
OVERLAP = 10


def collect_files(
    repo_path: Path,
    extensions: set[str] = INDEXABLE_EXTENSIONS,
    filenames: set[str] = INDEXABLE_FILENAMES,
) -> list[Path]:
    """Collect all indexable files from a repository."""
    files = []
    for path in repo_path.rglob("*"):
        # Check only the parts inside the repo. Checking the absolute path would
        # skip the whole repo whenever it sits under a directory named "data",
        # "build" and so on.
        relative_parts = path.relative_to(repo_path).parts
        if any(skip in relative_parts for skip in SKIP_DIRS):
            continue
        if not path.is_file():
            continue
        if path.suffix in extensions or path.name in filenames:
            files.append(path)
    return sorted(files)


def is_top_level_boundary(line: str) -> bool:
    """
    True if this line starts a new top-level unit.

    Splitting only on class/def used to lump every module-level constant into
    one chunk together with the docstring and imports. A query for any single
    constant then had to match a vector averaged over all of them.
    """
    if line.startswith("class ") or line.startswith("def "):
        return True

    # Module-level constant: NAME = ... at column 0, e.g. BLOCKED_COMMANDS = [
    if not line or line[0].isspace() or "=" in line[:1]:
        return False
    if "=" not in line:
        return False

    name = line.split("=")[0].strip()
    return name.isupper() and name.isidentifier()


def chunk_python(content: str, filepath: str, repo: str) -> list[dict[str, Any]]:
    """Chunk Python files by splitting on top-level definitions and constants."""
    lines = content.split("\n")
    chunks: list[dict[str, Any]] = []
    current_chunk_start = 0

    for i, line in enumerate(lines):
        # Split on top-level definitions (no leading whitespace)
        if i > 0 and is_top_level_boundary(line):
            chunk_content = "\n".join(lines[current_chunk_start:i]).strip()
            if chunk_content:
                chunks.append(
                    {
                        "content": chunk_content,
                        "filepath": filepath,
                        "start_line": current_chunk_start + 1,
                        "end_line": i,
                        "repo": repo,
                    }
                )
            current_chunk_start = i

    # Don't forget the last chunk
    chunk_content = "\n".join(lines[current_chunk_start:]).strip()
    if chunk_content:
        chunks.append(
            {
                "content": chunk_content,
                "filepath": filepath,
                "start_line": current_chunk_start + 1,
                "end_line": len(lines),
                "repo": repo,
            }
        )

    return chunks


def chunk_generic(content: str, filepath: str, repo: str) -> list[dict[str, Any]]:
    """Chunk non-Python files by fixed line count with overlap."""
    lines = content.split("\n")
    chunks: list[dict[str, Any]] = []

    i = 0
    while i < len(lines):
        end = min(i + CHUNK_SIZE, len(lines))
        chunk_content = "\n".join(lines[i:end]).strip()
        if chunk_content:
            chunks.append(
                {
                    "content": chunk_content,
                    "filepath": filepath,
                    "start_line": i + 1,
                    "end_line": end,
                    "repo": repo,
                }
            )
        i += CHUNK_SIZE - OVERLAP

    return chunks


def chunk_file(path: Path, repo_path: Path, repo_name: str) -> list[dict[str, Any]]:
    """Chunk a single file using the appropriate strategy."""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.warning("Could not read %s: %s", path, e)
        return []

    if not content.strip():
        return []

    # Limit very large files
    if len(content) > 100_000:
        content = content[:100_000]

    filepath = str(path.relative_to(repo_path))

    if path.suffix == ".py":
        return chunk_python(content, filepath, repo_name)
    return chunk_generic(content, filepath, repo_name)


def chunk_repository(
    repo_path: Path,
    repo_name: str,
    extensions: set[str] = INDEXABLE_EXTENSIONS,
    filenames: set[str] = INDEXABLE_FILENAMES,
) -> list[dict[str, Any]]:
    """Chunk all files in a repository."""
    files = collect_files(repo_path, extensions, filenames)
    logger.info("Found %d indexable files in %s", len(files), repo_path)

    all_chunks: list[dict[str, Any]] = []
    for path in files:
        all_chunks.extend(chunk_file(path, repo_path, repo_name))

    logger.info("Created %d chunks from %d files", len(all_chunks), len(files))
    return all_chunks
