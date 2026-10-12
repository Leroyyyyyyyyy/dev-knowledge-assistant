"""
The only code that talks to GitHub. It sorts every failure into one of two kinds,
because they call for opposite handling:

- IssueNotCreated: GitHub certainly did not create the Issue (nothing was sent,
  or GitHub refused it). Safe to fix and submit again.
- IssueOutcomeUnknown: the request may have reached GitHub (timeout after
  sending, a lost response, a 5xx). Submitting again could create a duplicate,
  so the draft must be reconciled first.

Creating an Issue is never retried here: a retry is exactly how a lost response
turns into two Issues.
"""

from dataclasses import dataclass
from typing import Protocol

import httpx

API_VERSION = "2022-11-28"
# How far back reconciliation looks for a draft's marker: 3 pages of 100.
LOOKUP_PAGES = 3


@dataclass
class CreatedIssue:
    number: int
    url: str


class IssueNotCreated(Exception):
    """GitHub did not create the Issue. The message says what to fix."""


class IssueOutcomeUnknown(Exception):
    """The Issue may or may not exist. Reconcile before doing anything else."""


class IssueLookupFailed(Exception):
    """Reconciliation could not read the repository's Issues."""


class GitHubClient(Protocol):
    def create_issue(self, repo: str, title: str, body: str) -> CreatedIssue: ...

    def find_issues_with_marker(self, repo: str, marker: str) -> list[CreatedIssue]: ...


REFUSALS = {
    401: "GitHub 拒绝了 token（401）：token 无效或已过期，换一个 token 后再提交。",
    403: "GitHub 拒绝了请求（403）：token 没有这个仓库的 Issues 写权限，或者触发了频率限制。检查 token 权限，稍后再试。",
    404: "GitHub 找不到仓库（404）：仓库名不对，或者 token 没有被授权访问这个仓库。",
    410: "这个仓库关闭了 Issues（410），在仓库设置里打开后再提交。",
    422: "GitHub 认为内容不合法（422），通常是标题为空或过长。修改后再提交。",
}


class HttpGitHubClient:
    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,  # tests pass a MockTransport
    ) -> None:
        # The token goes into a header and nowhere else: not in URLs, logs or messages.
        self._client = httpx.Client(
            base_url=api_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )

    def create_issue(self, repo: str, title: str, body: str) -> CreatedIssue:
        try:
            response = self._client.post(f"/repos/{repo}/issues", json={"title": title, "body": body})
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # No connection was made, so no request was sent.
            raise IssueNotCreated(f"连不上 GitHub（{type(exc).__name__}），请求没有发出。检查网络后再提交。") from exc
        except httpx.HTTPError as exc:
            # Sent, but the answer never arrived: GitHub may have created it.
            raise IssueOutcomeUnknown(f"请求已发出但没有收到完整响应（{type(exc).__name__}）。") from exc

        if response.status_code == 201:
            data = response.json()
            return CreatedIssue(number=data["number"], url=data["html_url"])
        if response.status_code in REFUSALS:
            raise IssueNotCreated(REFUSALS[response.status_code])
        if 400 <= response.status_code < 500:
            raise IssueNotCreated(f"GitHub 拒绝了请求（{response.status_code}）。")
        raise IssueOutcomeUnknown(f"GitHub 返回 {response.status_code}，不能确定 Issue 是否已创建。")

    def find_issues_with_marker(self, repo: str, marker: str) -> list[CreatedIssue]:
        # The Issues list, not the search API: search is indexed with a delay,
        # so an Issue created a minute ago might not show up there yet.
        found = []
        for page in range(1, LOOKUP_PAGES + 1):
            try:
                response = self._client.get(
                    f"/repos/{repo}/issues",
                    params={"state": "all", "sort": "created", "direction": "desc", "per_page": 100, "page": page},
                )
            except httpx.HTTPError as exc:
                raise IssueLookupFailed(f"读取 Issue 列表失败（{type(exc).__name__}）。") from exc
            if response.status_code != 200:
                raise IssueLookupFailed(f"读取 Issue 列表失败（HTTP {response.status_code}）。")
            items = response.json()
            for item in items:
                if "pull_request" in item:
                    continue  # the Issues list also returns pull requests
                if marker in (item.get("body") or ""):
                    found.append(CreatedIssue(number=item["number"], url=item["html_url"]))
            if len(items) < 100:
                break
        return found
