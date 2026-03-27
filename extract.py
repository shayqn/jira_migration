"""
extract.py – Pull issues from Workspace A via the Jira Cloud REST API.

Usage:
  from config import load_config
  from extract import fetch_all_issues

  cfg = load_config()
  issues = fetch_all_issues(cfg.jira_a, project_key="PROJ1", page_size=100)
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, Generator, List, Optional

import requests
from requests.auth import HTTPBasicAuth

from config import SiteConfig

# Fields requested from the API.  Add more here and they'll appear in the raw
# issue dict; transform.py decides which ones end up in the CSV.
# Note: "subtasks" and "comment" are not valid field keys for the search API.
REQUESTED_FIELDS = [
    "summary",
    "description",
    "issuetype",
    "status",
    "priority",
    "reporter",
    "assignee",
    "created",
    "updated",
    "resolution",
    "resolutiondate",
    "labels",
    "components",
    "parent",       # sub-task parent link
    "duedate",
    "customfield_10015",  # Start date (most Jira Cloud projects)
]


def _make_auth(site: SiteConfig) -> HTTPBasicAuth:
    return HTTPBasicAuth(site.email, site.api_token)


def _make_headers() -> Dict[str, str]:
    return {"Accept": "application/json", "Content-Type": "application/json"}


def _get_json(url: str, params: Dict[str, Any], auth: HTTPBasicAuth, retries: int = 3) -> Any:
    """GET a URL with simple retry logic for transient 429/5xx errors."""
    headers = _make_headers()
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, auth=auth, timeout=30)
        except requests.RequestException as exc:
            if attempt == retries:
                raise
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            print(f"[extract] Rate limited – waiting {retry_after}s …", file=sys.stderr)
            time.sleep(retry_after)
            continue

        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            print(f"[extract] HTTP {resp.status_code} error body: {detail}", file=sys.stderr)
            resp.raise_for_status()

        return resp.json()

    raise RuntimeError(f"Failed to GET {url} after {retries} retries.")


def _post_json(url: str, body: Dict[str, Any], auth: HTTPBasicAuth, retries: int = 3) -> Any:
    """POST a JSON body with simple retry logic for transient 429/5xx errors."""
    headers = _make_headers()
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=body, headers=headers, auth=auth, timeout=30)
        except requests.RequestException as exc:
            if attempt == retries:
                print(f"[extract] Network error after {retries} attempts: {exc}", file=sys.stderr)
                raise
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            print(f"[extract] Rate limited – waiting {retry_after}s …", file=sys.stderr)
            time.sleep(retry_after)
            continue

        if resp.status_code == 401:
            raise PermissionError(
                "Jira returned 401 Unauthorized. Check JIRA_A_EMAIL and JIRA_A_API_TOKEN."
            )
        if resp.status_code == 403:
            raise PermissionError(
                f"Jira returned 403 Forbidden for {url}. "
                "Your token may lack Browse Projects permission."
            )

        if not resp.ok:
            # Print Jira's error detail before raising so it's visible in the traceback.
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            print(f"[extract] HTTP {resp.status_code} error body: {detail}", file=sys.stderr)
            resp.raise_for_status()

        return resp.json()

    raise RuntimeError(f"Failed to POST {url} after {retries} retries.")


def _iter_pages(
    site: SiteConfig,
    jql: str,
    page_size: int,
) -> Generator[List[Dict[str, Any]], None, None]:
    """
    Yield pages of raw Jira issue dicts, handling pagination automatically.
    Each page is a list of issue dicts from the 'issues' key of the response.
    """
    # /rest/api/3/search (GET and POST) is deprecated → 410 Gone.
    # POST /rest/api/3/search/jql uses cursor-based pagination via nextPageToken.
    url = f"{site.base_url}/rest/api/3/search/jql"
    auth = _make_auth(site)
    next_page_token: str | None = None

    while True:
        body: Dict[str, Any] = {
            "jql": jql,
            "maxResults": page_size,
            "fields": REQUESTED_FIELDS,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        data = _post_json(url, body, auth)

        issues: List[Dict[str, Any]] = data.get("issues", [])

        if not issues:
            break

        yield issues

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break


def fetch_all_issues(
    site: SiteConfig,
    project_key: str,
    page_size: int = 100,
    jql_extra: str = "",
) -> List[Dict[str, Any]]:
    """
    Fetch every issue for *project_key* from *site*.

    Args:
      site        – SiteConfig for Workspace A (base_url, email, api_token).
      project_key – Jira project key, e.g. "PROJ1".
      page_size   – Issues per API request (max 100 per Jira Cloud limit).
      jql_extra   – Optional extra JQL clauses appended with AND, e.g.
                    'AND issuetype != Sub-task'.

    Returns:
      Flat list of raw Jira issue dicts (the objects inside the 'issues' array).
    """
    jql = f"project = {project_key} ORDER BY created ASC"
    if jql_extra:
        jql = f"project = {project_key} AND ({jql_extra}) ORDER BY created ASC"

    all_issues: List[Dict[str, Any]] = []
    page_num = 0

    print(f"[extract] Fetching issues for project '{project_key}' from {site.base_url} …")

    for page in _iter_pages(site, jql, page_size):
        page_num += 1
        all_issues.extend(page)
        print(f"[extract]   Page {page_num}: fetched {len(page)} issues "
              f"(total so far: {len(all_issues)})")

    print(f"[extract] Done – {len(all_issues)} issues fetched.")
    return all_issues


def fetch_comments(
    site: SiteConfig,
    issue_key: str,
) -> List[Dict[str, Any]]:
    """
    Fetch all comments for a single issue, handling pagination.

    Returns a list of raw Jira comment dicts from the 'comments' array,
    each containing at minimum: id, author, body (ADF), created, updated.
    """
    url = f"{site.base_url}/rest/api/3/issue/{issue_key}/comment"
    auth = _make_auth(site)
    all_comments: List[Dict[str, Any]] = []
    start_at = 0
    page_size = 100

    while True:
        data = _get_json(url, {"startAt": start_at, "maxResults": page_size}, auth)
        comments: List[Dict[str, Any]] = data.get("comments", [])
        total: int = data.get("total", 0)

        all_comments.extend(comments)
        start_at += len(comments)

        if not comments or start_at >= total:
            break

    return all_comments


def fetch_comments_for_issues(
    site: SiteConfig,
    issues: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Fetch comments for every issue in *issues* and return a dict of
    {issue_key: [comment, ...]} for issues that have at least one comment.

    Skips issues with zero comments to avoid unnecessary API calls.
    """
    result: Dict[str, List[Dict[str, Any]]] = {}
    issues_with_comments = [
        i for i in issues
        if (i.get("fields") or {}).get("comment", {}).get("total", 1) != 0
    ]
    total = len(issues)
    print(f"[extract] Fetching comments for {total} issues …")

    for idx, issue in enumerate(issues, start=1):
        key = issue.get("key", "")
        comments = fetch_comments(site, key)
        if comments:
            result[key] = comments
        if idx % 50 == 0 or idx == total:
            print(f"[extract]   {idx}/{total} issues processed for comments")

    total_comments = sum(len(v) for v in result.values())
    print(f"[extract] Done – {total_comments} comments fetched across {len(result)} issues.")
    return result
