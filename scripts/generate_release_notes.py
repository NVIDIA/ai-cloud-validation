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
from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class MilestoneInfo:
    """Information about a GitHub milestone."""

    org: str
    repo: str
    milestone_number: int
    title: str
    description: str = ""


@dataclass
class IssueInfo:
    """Information about a GitHub issue or pull request."""

    number: int
    title: str
    url: str
    is_pr: bool
    labels: list[str]
    draft: bool = False


def _issue_bullet(issue: IssueInfo, *, show_draft: bool = False) -> str:
    """Format an issue/PR as a markdown bullet line.

    show_draft only matters for PRs in the type-grouping path, which surfaces drafts.
    """
    prefix = "PR" if issue.is_pr else "Issue"
    draft_suffix = " (draft)" if show_draft and issue.is_pr and issue.draft else ""
    return f"- {prefix} #{issue.number}: [{issue.title}]({issue.url}){draft_suffix}"


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
        url = f"{self.base_url}{endpoint}"
        response = requests.get(url, headers=self.headers, params=params, timeout=30)
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            raise requests.exceptions.HTTPError(
                f"{response.status_code} Client Error: {_format_http_error(response)} for url: {url}",
                response=response,
            )
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
                response.raise_for_status()
            except requests.exceptions.HTTPError:
                if response.status_code == 403:
                    rate_limit_reset = response.headers.get("X-RateLimit-Reset")
                    if rate_limit_reset:
                        reset_time = datetime.datetime.fromtimestamp(int(rate_limit_reset))
                        print(f"\nRate limit exceeded. Resets at: {reset_time}", file=sys.stderr)
                raise requests.exceptions.HTTPError(
                    f"{response.status_code} Client Error: {_format_http_error(response)} for url: {response.url}",
                    response=response,
                )
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
    )


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

    filtered_issues = issues
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
            if issue.labels:
                # Filter out excluded labels, then use first remaining label for grouping
                available_labels = [lbl for lbl in issue.labels if lbl not in exclude_labels_set]
                if available_labels:
                    label = available_labels[0]
                    if label not in label_groups:
                        label_groups[label] = []
                    label_groups[label].append(issue)
                else:
                    # All labels were excluded, treat as unlabeled
                    unlabeled.append(issue)
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
    lines.append("---")
    lines.append("")
    lines.append(f"**Total**: {len(filtered_issues)} items ({pr_count} PRs, {issue_count} issues)")

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
