#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests>=2.32.5",
# ]
# ///
"""
Generate release notes from a GitHub milestone.

Fetches issues and pull requests associated with a GitHub milestone and generates
a formatted markdown release notes document with links and titles.

Each issue carries the merged PRs that closed it on its own line, so a PR is
listed separately only when it closes no issue being reported on.

A milestone only covers what someone assigned to it. Pass --since (optionally
with --until) to also pull in every merged PR between two refs along with the
issues those PRs closed, which is what a release spanning several tags needs.

Usage:
    uv run scripts/generate_release_notes.py <milestone_url> [options]

    Options:
        --token, -t          GitHub personal access token (or use GITHUB_TOKEN env var)
        --output, -o         Output file path (default: stdout)
        --group-by {label,type}
                             Group items by label or by type (PRs vs Issues). Default: label
        --include-open       Include open issues/PRs (default: only closed)
        --exclude-draft      Exclude draft pull requests (default: include all)
        --exclude-label      Exclude label from grouping (can be specified multiple times)
        --since              Also include merged PRs after this git ref, plus the issues they closed
        --until              End ref for --since (default: HEAD)

Authentication:
    Set GITHUB_TOKEN or pass --token. Quick path via the gh CLI:
        export GITHUB_TOKEN=$(gh auth token)

Example:
    uv run scripts/generate_release_notes.py https://github.com/NVIDIA/ai-cloud-validation/milestone/1
    uv run scripts/generate_release_notes.py https://github.com/org/repo/milestone/1 --output release-notes.md
    uv run scripts/generate_release_notes.py https://github.com/org/repo/milestone/1 --group-by type
"""

import argparse
import datetime
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import requests

# Issues per GraphQL query when resolving implementing pull requests.
GRAPHQL_BATCH = 50

PR_FIELDS = """
    number title url body merged
    labels(first: 20) { nodes { name } }
    closingIssuesReferences(first: 10) {
        nodes { number title url state labels(first: 20) { nodes { name } } }
    }
"""

ISSUE_CLOSER_FIELDS = """
    closedByPullRequestsReferences(first: 10, includeClosedPrs: true) { nodes { number url state } }
"""

# Release mechanics carry no user-visible change, matching the CHANGELOG prompt's
# instruction to skip the bump commit.
BUMP_TITLE_PREFIX = "chore: update package versions"

# Conventional-commit prefix, e.g. "fix(breakfix): ..." or "docs: ...".
CONVENTIONAL_TITLE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?!?:")
DOC_TYPES = {"doc", "docs"}


@dataclass
class MilestoneInfo:
    """Information about a GitHub milestone."""

    org: str
    repo: str
    milestone_number: int
    title: str
    description: str = ""


@dataclass
class PullRequestRef:
    """A merged pull request that closed an issue."""

    number: int
    url: str


@dataclass
class IssueInfo:
    """Information about a GitHub issue or pull request."""

    number: int
    title: str
    url: str
    is_pr: bool
    labels: list[str]
    draft: bool = False
    body: str = ""
    closing_prs: list[PullRequestRef] = field(default_factory=list)


def _issue_bullet(issue: IssueInfo, *, show_draft: bool = False) -> str:
    """Format an issue/PR as a markdown bullet line.

    show_draft only matters for PRs in the type-grouping path, which surfaces drafts.
    """
    prefix = "PR" if issue.is_pr else "Issue"
    draft_suffix = " (draft)" if show_draft and issue.is_pr and issue.draft else ""
    pr_suffix = ""
    if issue.closing_prs:
        links = ", ".join(f"[PR #{pr.number}]({pr.url})" for pr in issue.closing_prs)
        pr_suffix = f" ({links})"
    return f"- {prefix} #{issue.number}: [{issue.title}]({issue.url}){draft_suffix}{pr_suffix}"


def _format_http_error(response: requests.Response) -> str:
    """Extract a human-readable error message from a GitHub API error response."""
    error_msg = response.text
    try:
        error_json = response.json()
    except ValueError:
        return error_msg
    if "message" in error_json:
        error_msg = error_json["message"]
    if "errors" in error_json:
        error_details = "; ".join([str(e) for e in error_json["errors"]])
        error_msg = f"{error_msg} ({error_details})"
    return error_msg


def _raise_for_status(response: requests.Response) -> None:
    """Re-raise a failed response with GitHub's own error message in the text."""
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        raise requests.exceptions.HTTPError(
            f"{response.status_code} Client Error: {_format_http_error(response)} for url: {response.url}",
            response=response,
        )


class GitHubAPI:
    """GitHub API client."""

    def __init__(self, token: str | None = None) -> None:
        """Initialize GitHub API client."""
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.token:
            raise ValueError("GitHub token is required. Set GITHUB_TOKEN environment variable or use --token option.")
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }
        self.base_url = "https://api.github.com"

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a GET request to GitHub API."""
        response = requests.get(f"{self.base_url}{endpoint}", headers=self.headers, params=params, timeout=30)
        _raise_for_status(response)
        return response.json()

    def _get_paginated(
        self, endpoint: str, params: dict[str, Any] | None = None, verbose: bool = False
    ) -> list[dict[str, Any]]:
        """Get paginated results from GitHub API."""
        all_items = []
        page = 1
        per_page = 100

        while True:
            if params is None:
                params = {}
            params["page"] = page
            params["per_page"] = per_page

            if verbose and page > 1:
                print(f"    Fetching page {page}...", file=sys.stderr)

            response = requests.get(f"{self.base_url}{endpoint}", headers=self.headers, params=params, timeout=30)

            remaining = response.headers.get("X-RateLimit-Remaining")
            if remaining and int(remaining) < 10:
                print(f"    Rate limit warning: {remaining} requests remaining", file=sys.stderr)

            try:
                _raise_for_status(response)
            except requests.exceptions.HTTPError:
                if response.status_code == 403:
                    rate_limit_reset = response.headers.get("X-RateLimit-Reset")
                    if rate_limit_reset:
                        reset_time = datetime.datetime.fromtimestamp(int(rate_limit_reset))
                        print(f"\nRate limit exceeded. Resets at: {reset_time}", file=sys.stderr)
                raise
            items = response.json()

            if not items:
                break

            all_items.extend(items)
            if verbose:
                print(f"    Page {page}: {len(items)} items (total: {len(all_items)})", file=sys.stderr)

            if len(items) < per_page:
                break

            page += 1

        return all_items

    def get_milestone(self, org: str, repo: str, milestone_number: int) -> dict[str, Any]:
        """Get milestone information."""
        endpoint = f"/repos/{org}/{repo}/milestones/{milestone_number}"
        return self._get(endpoint)

    def get_milestone_issues(
        self, org: str, repo: str, milestone_number: int, state: str = "all", verbose: bool = False
    ) -> list[dict[str, Any]]:
        """Get all issues and PRs for a milestone."""
        endpoint = f"/repos/{org}/{repo}/issues"
        params = {"milestone": str(milestone_number), "state": state}
        return self._get_paginated(endpoint, params, verbose=verbose)

    def _post_graphql(self, query: str) -> dict[str, Any]:
        """Run a GraphQL query and return its data payload."""
        response = requests.post(f"{self.base_url}/graphql", headers=self.headers, json={"query": query}, timeout=30)
        _raise_for_status(response)
        payload = response.json()
        if "errors" in payload:
            messages = "; ".join(str(e.get("message", e)) for e in payload["errors"])
            raise ValueError(f"GraphQL error: {messages}")
        return payload["data"]

    def _batched_nodes(
        self, org: str, repo: str, numbers: list[int], field: str, selection: str
    ) -> Iterator[tuple[int, dict[str, Any] | None]]:
        """Look up numbered issues or PRs, aliasing GRAPHQL_BATCH of them per query."""
        for start in range(0, len(numbers), GRAPHQL_BATCH):
            batch = numbers[start : start + GRAPHQL_BATCH]
            aliases = "\n".join(f"n{number}: {field}(number: {number}) {{ {selection} }}" for number in batch)
            data = self._post_graphql(f'{{ repository(owner: "{org}", name: "{repo}") {{ {aliases} }} }}')
            repository = data.get("repository") or {}
            for number in batch:
                yield number, repository.get(f"n{number}")

    def get_range_pr_numbers(self, org: str, repo: str, since: str, until: str) -> list[int]:
        """PR numbers referenced by the squash commits between two refs."""
        try:
            data = self._get(f"/repos/{org}/{repo}/compare/{since}...{until}")
        except requests.exceptions.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            if status == 404:
                raise ValueError(f"Cannot compare {since}...{until}: check that both refs exist in {org}/{repo}")
            raise
        commits = data.get("commits", [])
        total = data.get("total_commits", len(commits))
        if len(commits) < total:
            print(
                f"    Warning: compare returned {len(commits)} of {total} commits; "
                "narrow the range to avoid missing PRs",
                file=sys.stderr,
            )
        numbers = set()
        for commit in commits:
            subject = (commit.get("commit", {}).get("message") or "").split("\n", 1)[0]
            match = re.search(r"\(#(\d+)\)\s*$", subject)
            if match:
                numbers.add(int(match.group(1)))
        return sorted(numbers)

    def get_range_items(
        self, org: str, repo: str, pr_numbers: list[int], include_open: bool = False
    ) -> list[IssueInfo]:
        """Merged PRs from a ref range, plus the issues they close.

        The issues come back with the PRs because a range is defined by commits:
        an issue closed inside it need not carry the milestone being reported on.
        """
        items: dict[int, IssueInfo] = {}
        for _, pull in self._batched_nodes(org, repo, pr_numbers, "pullRequest", PR_FIELDS):
            if not pull or not pull.get("merged"):
                continue
            items[pull["number"]] = IssueInfo(
                number=pull["number"],
                title=pull["title"],
                url=pull["url"],
                is_pr=True,
                labels=[label["name"] for label in (pull.get("labels") or {}).get("nodes", [])],
                body=pull.get("body") or "",
            )
            for issue in (pull.get("closingIssuesReferences") or {}).get("nodes", []):
                if issue["state"] == "OPEN" and not include_open:
                    continue
                items.setdefault(
                    issue["number"],
                    IssueInfo(
                        number=issue["number"],
                        title=issue["title"],
                        url=issue["url"],
                        is_pr=False,
                        labels=[label["name"] for label in (issue.get("labels") or {}).get("nodes", [])],
                    ),
                )
        return list(items.values())

    def get_closing_prs(self, org: str, repo: str, issue_numbers: list[int]) -> dict[int, list[PullRequestRef]]:
        """Map each issue number to the merged pull requests that closed it.

        Unmerged PRs are dropped: an issue can reference a closed-without-merge
        attempt alongside the PR that actually landed.
        """
        closing: dict[int, list[PullRequestRef]] = {}
        for number, node in self._batched_nodes(org, repo, issue_numbers, "issue", ISSUE_CLOSER_FIELDS):
            refs = ((node or {}).get("closedByPullRequestsReferences") or {}).get("nodes") or []
            merged = [PullRequestRef(number=r["number"], url=r["url"]) for r in refs if r["state"] == "MERGED"]
            if merged:
                closing[number] = merged
        return closing


def parse_milestone_url(url: str) -> MilestoneInfo:
    """Parse a GitHub milestone URL to extract org, repo, and milestone number."""
    pattern = r"https://github\.com/([^/]+)/([^/]+)/milestone/(\d+)/?"
    match = re.fullmatch(pattern, url.strip())

    if not match:
        raise ValueError(
            f"Invalid milestone URL format: {url}\nExpected format: https://github.com/org/repo/milestone/1"
        )

    org = match.group(1)
    repo = match.group(2)
    milestone_number = int(match.group(3))

    return MilestoneInfo(org=org, repo=repo, milestone_number=milestone_number, title="")


def parse_issue(issue_data: dict[str, Any]) -> IssueInfo:
    """Parse GitHub issue/PR data into IssueInfo."""
    is_pr = "pull_request" in issue_data
    return IssueInfo(
        number=issue_data["number"],
        title=issue_data["title"],
        url=issue_data["html_url"],
        is_pr=is_pr,
        labels=[label["name"] for label in issue_data.get("labels", [])],
        draft=issue_data.get("draft", False) if is_pr else False,
        body=issue_data.get("body") or "",
    )


def _mentioned_issue_numbers(body: str, issue_numbers: set[int]) -> list[int]:
    """Issue numbers referenced in a PR body, restricted to the milestone's own issues."""
    referenced = {int(number) for number in re.findall(r"#(\d+)", body)}
    return sorted(referenced & issue_numbers)


def link_prs_to_issues(items: list[IssueInfo], closing: dict[int, list[PullRequestRef]]) -> list[IssueInfo]:
    """Move implementing PRs onto their issue's line and drop them as separate items.

    GitHub's closing links are authoritative. A PR that referenced its issue in
    prose instead of using a closing keyword declares no link at all, so fall
    back to the issue numbers mentioned in its body - restricted to this
    milestone's issues, which keeps references to unrelated work out.
    """
    issues_by_number = {item.number: item for item in items if not item.is_pr}
    for issue in issues_by_number.values():
        issue.closing_prs = list(closing.get(issue.number, []))

    linked = {pr.number for issue in issues_by_number.values() for pr in issue.closing_prs}
    issue_numbers = set(issues_by_number)

    for item in items:
        if not item.is_pr or item.number in linked:
            continue
        for number in _mentioned_issue_numbers(item.body, issue_numbers):
            issues_by_number[number].closing_prs.append(PullRequestRef(number=item.number, url=item.url))
            linked.add(item.number)

    for issue in issues_by_number.values():
        issue.closing_prs.sort(key=lambda pr: pr.number)

    return [item for item in items if not (item.is_pr and item.number in linked)]


def _scope_group(title: str) -> str | None:
    """Section for an item carrying no usable label, taken from its commit scope.

    PRs here are routinely unlabelled, so their conventional-commit title is the
    only signal left. Documentation changes group under one heading instead of
    splintering into a section per scope.
    """
    match = CONVENTIONAL_TITLE.match(title)
    if not match:
        return None
    if match.group("type") in DOC_TYPES:
        return "documentation"
    return match.group("scope")


def generate_markdown(
    milestone: MilestoneInfo,
    issues: list[IssueInfo],
    group_by: str = "label",
    exclude_draft: bool = False,
    exclude_labels: list[str] | None = None,
) -> str:
    """Generate markdown release notes."""
    lines = []

    lines.append(f"# {milestone.title}")
    lines.append("")
    if milestone.description:
        lines.append(milestone.description)
        lines.append("")

    filtered_issues = [i for i in issues if not i.title.startswith(BUMP_TITLE_PREFIX)]
    if exclude_draft:
        filtered_issues = [i for i in filtered_issues if not i.draft]

    if not filtered_issues:
        lines.append("*No items found in this milestone.*")
        return "\n".join(lines) + "\n"

    if group_by == "label":
        label_groups: dict[str, list[IssueInfo]] = {}
        unlabeled: list[IssueInfo] = []
        exclude_labels_set = set(exclude_labels or [])

        for issue in filtered_issues:
            # Filter out excluded labels, then use first remaining label for grouping
            available_labels = [lbl for lbl in issue.labels if lbl not in exclude_labels_set]
            group = available_labels[0] if available_labels else _scope_group(issue.title)
            if group:
                label_groups.setdefault(group, []).append(issue)
            else:
                unlabeled.append(issue)

        sorted_labels = sorted(label_groups.keys(), key=str.lower)

        for label in sorted_labels:
            lines.append(f"## {label}")
            lines.append("")
            for issue in sorted(label_groups[label], key=lambda x: x.number):
                lines.append(_issue_bullet(issue))
            lines.append("")

        if unlabeled:
            lines.append("## Uncategorized")
            lines.append("")
            for issue in sorted(unlabeled, key=lambda x: x.number):
                lines.append(_issue_bullet(issue))
            lines.append("")

    else:
        prs = [i for i in filtered_issues if i.is_pr]
        issues_only = [i for i in filtered_issues if not i.is_pr]

        if prs:
            lines.append("## Pull Requests")
            lines.append("")
            for pr in sorted(prs, key=lambda x: x.number):
                lines.append(_issue_bullet(pr, show_draft=True))
            lines.append("")

        if issues_only:
            lines.append("## Issues")
            lines.append("")
            for issue in sorted(issues_only, key=lambda x: x.number):
                lines.append(_issue_bullet(issue))
            lines.append("")

    pr_count = sum(1 for i in filtered_issues if i.is_pr)
    issue_count = len(filtered_issues) - pr_count
    # Distinct PRs: one PR can close several issues and appear on each of their lines.
    linked_pr_count = len({pr.number for i in filtered_issues for pr in i.closing_prs})
    total = f"**Total**: {len(filtered_issues)} items ({pr_count} PRs, {issue_count} issues)"
    if linked_pr_count:
        total += f", plus {linked_pr_count} implementing PRs linked inline"
    lines.append("---")
    lines.append("")
    lines.append(total)

    return "\n".join(lines) + "\n"


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate release notes from a GitHub milestone",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "milestone_url",
        help="GitHub milestone URL (e.g., https://github.com/org/repo/milestone/1)",
    )
    parser.add_argument(
        "-t",
        "--token",
        help="GitHub personal access token (or use GITHUB_TOKEN env var)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=argparse.FileType("w"),
        default=sys.stdout,
        help="Output file path (default: stdout)",
    )
    parser.add_argument(
        "--group-by",
        choices=["label", "type"],
        default="label",
        help="Group items by label or by type (PRs vs Issues). Default: label",
    )
    parser.add_argument(
        "--include-open",
        action="store_true",
        help="Include open issues/PRs (default: only closed - suitable for release notes)",
    )
    parser.add_argument(
        "--exclude-draft",
        action="store_true",
        help="Exclude draft pull requests",
    )
    parser.add_argument(
        "--exclude-label",
        action="append",
        default=[],
        help="Exclude label from grouping (can be specified multiple times). "
        "When grouping by label, excluded labels are skipped. "
        "Example: --exclude-label test-scripts --exclude-label enhancement",
    )
    parser.add_argument(
        "--since",
        help="Also include merged PRs after this git ref (tag, branch, or SHA), plus the issues they closed. "
        "Use when a release spans work that was never milestoned.",
    )
    parser.add_argument(
        "--until",
        default="HEAD",
        help="End ref for --since (default: HEAD)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show verbose output including API debugging info",
    )

    args = parser.parse_args()

    try:
        milestone_info = parse_milestone_url(args.milestone_url)
        api = GitHubAPI(token=args.token)

        print("Fetching milestone information...", file=sys.stderr)
        milestone_data = api.get_milestone(milestone_info.org, milestone_info.repo, milestone_info.milestone_number)
        milestone_info.title = milestone_data.get("title", f"Milestone {milestone_info.milestone_number}")
        milestone_info.description = milestone_data.get("description", "")

        if args.verbose:
            print(f"  Milestone: {milestone_info.title} (#{milestone_info.milestone_number})", file=sys.stderr)
            print(f"  Repository: {milestone_info.org}/{milestone_info.repo}", file=sys.stderr)
            print(f"  Milestone state: {milestone_data.get('state', 'unknown')}", file=sys.stderr)
            print(f"  Milestone open issues: {milestone_data.get('open_issues', 0)}", file=sys.stderr)
            print(f"  Milestone closed issues: {milestone_data.get('closed_issues', 0)}", file=sys.stderr)

        issues_data = []
        print("Fetching closed issues and PRs...", file=sys.stderr)
        closed_issues = api.get_milestone_issues(
            milestone_info.org,
            milestone_info.repo,
            milestone_info.milestone_number,
            state="closed",
            verbose=args.verbose,
        )
        issues_data.extend(closed_issues)
        if args.verbose:
            print(f"  Found {len(closed_issues)} closed items", file=sys.stderr)

        if args.include_open:
            print("Fetching open issues and PRs...", file=sys.stderr)
            open_issues = api.get_milestone_issues(
                milestone_info.org,
                milestone_info.repo,
                milestone_info.milestone_number,
                state="open",
                verbose=args.verbose,
            )
            issues_data.extend(open_issues)
            if args.verbose:
                print(f"  Found {len(open_issues)} open items", file=sys.stderr)

        issues = [parse_issue(issue) for issue in issues_data]
        print(f"Found {len(issues)} items", file=sys.stderr)

        if args.since:
            print(f"Fetching merged PRs in {args.since}...{args.until}...", file=sys.stderr)
            pr_numbers = api.get_range_pr_numbers(milestone_info.org, milestone_info.repo, args.since, args.until)
            range_items = api.get_range_items(
                milestone_info.org, milestone_info.repo, pr_numbers, include_open=args.include_open
            )
            known = {item.number for item in issues}
            added = [item for item in range_items if item.number not in known]
            issues.extend(added)
            print(f"  Added {len(added)} items not on the milestone", file=sys.stderr)

        issue_items = [i for i in issues if not i.is_pr]
        if issue_items:
            print("Resolving implementing pull requests...", file=sys.stderr)
            closing = api.get_closing_prs(milestone_info.org, milestone_info.repo, [i.number for i in issue_items])
            listed = len(issues)
            issues = link_prs_to_issues(issues, closing)
            if args.verbose:
                print(f"  Linked {listed - len(issues)} pull requests to issues", file=sys.stderr)

        if len(issues) == 0 and milestone_data.get("closed_issues", 0) > 0:
            print(
                "\nWarning: Milestone shows closed issues but API returned none. Re-run with --verbose for details.",
                file=sys.stderr,
            )

        markdown = generate_markdown(
            milestone_info,
            issues,
            group_by=args.group_by,
            exclude_draft=args.exclude_draft,
            exclude_labels=args.exclude_label or None,
        )

        args.output.write(markdown)
        if args.output != sys.stdout:
            args.output.close()
            print(f"Release notes written to {args.output.name}", file=sys.stderr)

        return 0

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        print(f"GitHub API error: {e}", file=sys.stderr)
        if status == 401:
            print("Authentication failed. Check your GitHub token.", file=sys.stderr)
        elif status == 403:
            print("Access forbidden. Token likely lacks 'Issues: Read' on the repository.", file=sys.stderr)
        elif status == 404:
            print("Milestone not found. Check the URL and your access permissions.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
