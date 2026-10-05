"""
Source links, built by the backend from chunk metadata.

The model never writes a URL. Links always name the full commit, never a branch,
so they keep pointing at the lines that were indexed after the branch moves on.
"""

from urllib.parse import quote

GITHUB = "https://github.com/"


def source_url(remote: str, commit_sha: str, path: str, start_line: int, end_line: int) -> str:
    if not remote.startswith(GITHUB):
        raise ValueError(f"only GitHub remotes are supported, got {remote!r}")
    if len(commit_sha) != 40:
        raise ValueError("citations need the full 40-character commit sha")
    # Encode each path segment (spaces, '#', '?', non-ASCII) but keep the '/' separators.
    encoded_path = quote(path, safe="/")
    anchor = f"L{start_line}" if start_line == end_line else f"L{start_line}-L{end_line}"
    return f"{remote.rstrip('/')}/blob/{commit_sha}/{encoded_path}#{anchor}"
