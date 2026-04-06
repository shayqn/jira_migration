#!/usr/bin/env python3
"""
backfill_user.py – Fix reporter/assignee for issues where a user was not mapped
during migration.

Searches a destination project for issues whose description contains a specific
display name in the legacy user block (e.g. "Original assignee: Erik MacLennan"),
then updates the reporter and/or assignee fields to the correct Workspace B user.

Usage:
  python backfill_user.py --project DESTPROJ --name "Erik MacLennan" --email erik.macLennan@epianeuro.com

Options:
  --project, -p    Destination project key in Workspace B (required)
  --name           Display name to search for in issue descriptions (required)
  --email          Target email address in Workspace B to assign (required)
  --dry-run        Print what would be changed without making any updates
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import os

import requests
from requests.auth import HTTPBasicAuth

from config import SiteConfig


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _auth(site: SiteConfig) -> HTTPBasicAuth:
    return HTTPBasicAuth(site.email, site.api_token)


def _headers() -> Dict[str, str]:
    return {"Accept": "application/json", "Content-Type": "application/json"}


def _request(
    method: str,
    url: str,
    site: SiteConfig,
    *,
    json: Any = None,
    params: Dict[str, Any] | None = None,
) -> Any:
    for attempt in range(1, 4):
        try:
            resp = requests.request(
                method, url, json=json, params=params,
                headers=_headers(), auth=_auth(site), timeout=30,
            )
        except requests.RequestException as exc:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10))
            print(f"Rate limited – waiting {wait}s …", file=sys.stderr)
            time.sleep(wait)
            continue

        if resp.status_code == 204:
            return {}

        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise requests.HTTPError(
                f"HTTP {resp.status_code} {method} {url} — {detail}",
                response=resp,
            )

        return resp.json()

    raise RuntimeError(f"Failed {method} {url} after 3 retries.")


# ---------------------------------------------------------------------------
# ADF text extraction
# ---------------------------------------------------------------------------

def _extract_text_lines(node: Any) -> List[str]:
    """
    Recursively collect all text content from an ADF node.
    Returns a flat list of non-empty strings, one per text node.
    """
    if not isinstance(node, dict):
        return []
    if node.get("type") == "text":
        text = node.get("text", "").strip()
        return [text] if text else []
    lines = []
    for child in node.get("content") or []:
        lines.extend(_extract_text_lines(child))
    return lines


def _detect_roles(description: Any, name: str) -> Tuple[bool, bool]:
    """
    Parse an ADF description and return (is_reporter, is_assignee) indicating
    whether *name* appears in the "Original reporter:" or "Original assignee:" lines.
    """
    lines = _extract_text_lines(description)
    reporter_marker = f"Original reporter: {name}".lower()
    assignee_marker = f"Original assignee: {name}".lower()
    is_reporter = any(reporter_marker in line.lower() for line in lines)
    is_assignee = any(assignee_marker in line.lower() for line in lines)
    return is_reporter, is_assignee


# ---------------------------------------------------------------------------
# Workspace B user lookup
# ---------------------------------------------------------------------------

def _lookup_account_id(site: SiteConfig, email: str) -> Optional[str]:
    """Return the accountId for *email* in Workspace B, or None if not found."""
    url = f"{site.base_url}/rest/api/3/user/search"
    results = _request("GET", url, site, params={"query": email})
    for user in results:
        if isinstance(user, dict) and user.get("emailAddress", "").lower() == email.lower():
            return user.get("accountId")
    # Fallback: take first result if email field is hidden
    if results and isinstance(results[0], dict):
        account_id = results[0].get("accountId")
        if account_id:
            print(
                f"Note: exact email match not found for '{email}'; "
                f"using first result (accountId={account_id})."
            )
            return account_id
    return None


# ---------------------------------------------------------------------------
# Issue search (paginated JQL)
# ---------------------------------------------------------------------------

def _search_issues(site: SiteConfig, project: str, name: str) -> List[Dict[str, Any]]:
    """
    Return all issues in *project* whose description contains *name*.
    Uses POST /rest/api/3/search/jql with cursor pagination.
    """
    url = f"{site.base_url}/rest/api/3/search/jql"
    # Escape quotes in name for JQL
    name_jql = name.replace('"', '\\"')
    jql = f'project = "{project}" AND description ~ "{name_jql}" ORDER BY created ASC'
    print(f"Searching: {jql}")

    all_issues: List[Dict[str, Any]] = []
    next_page_token: Optional[str] = None

    while True:
        body: Dict[str, Any] = {
            "jql": jql,
            "maxResults": 100,
            "fields": ["summary", "description", "reporter", "assignee"],
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        data = _request("POST", url, site, json=body)
        issues = data.get("issues") or []
        all_issues.extend(issues)
        print(f"  Fetched {len(all_issues)} issue(s) so far …")

        next_page_token = data.get("nextPageToken")
        if not next_page_token or not issues:
            break

    return all_issues


# ---------------------------------------------------------------------------
# Issue update
# ---------------------------------------------------------------------------

def _update_issue(
    site: SiteConfig,
    issue_key: str,
    account_id: str,
    set_reporter: bool,
    set_assignee: bool,
    dry_run: bool,
) -> None:
    fields: Dict[str, Any] = {}
    if set_reporter:
        fields["reporter"] = {"accountId": account_id}
    if set_assignee:
        fields["assignee"] = {"accountId": account_id}

    roles = []
    if set_reporter:
        roles.append("reporter")
    if set_assignee:
        roles.append("assignee")
    role_str = " + ".join(roles)

    if dry_run:
        print(f"  [dry-run] Would update {issue_key}: set {role_str}")
        return

    url = f"{site.base_url}/rest/api/3/issue/{issue_key}"
    _request("PUT", url, site, json={"fields": fields})
    print(f"  Updated {issue_key}: set {role_str}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill reporter/assignee for issues where a user was not mapped during migration."
    )
    parser.add_argument("--project", "-p", required=True,
                        help="Destination project key in Workspace B")
    parser.add_argument("--name", required=True,
                        help='Display name to search for, e.g. "Erik MacLennan"')
    parser.add_argument("--email", required=True,
                        help="Target email address in Workspace B")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be changed without making any updates")
    args = parser.parse_args()

    b_url   = os.environ.get("JIRA_B_BASE_URL", "").rstrip("/")
    b_email = os.environ.get("JIRA_B_EMAIL", "")
    b_token = os.environ.get("JIRA_B_API_TOKEN", "")
    if not (b_url and b_email and b_token):
        print("ERROR: Set JIRA_B_BASE_URL, JIRA_B_EMAIL, and JIRA_B_API_TOKEN "
              "environment variables before running this script.", file=sys.stderr)
        sys.exit(1)
    site = SiteConfig(base_url=b_url, email=b_email, api_token=b_token)

    if args.dry_run:
        print("--- DRY RUN — no changes will be made ---")

    # Resolve target accountId
    print(f"Looking up '{args.email}' in {site.base_url} …")
    account_id = _lookup_account_id(site, args.email)
    if not account_id:
        print(f"ERROR: Could not find a Workspace B account for '{args.email}'.", file=sys.stderr)
        sys.exit(1)
    print(f"Resolved accountId: {account_id}")

    # Find matching issues
    issues = _search_issues(site, args.project, args.name)
    if not issues:
        print(f"No issues found in '{args.project}' containing '{args.name}' in description.")
        return

    print(f"\nFound {len(issues)} candidate issue(s). Checking descriptions …\n")

    updated = 0
    skipped = 0

    for issue in issues:
        key = issue.get("key", "?")
        fields = issue.get("fields") or {}
        description = fields.get("description")

        is_reporter, is_assignee = _detect_roles(description, args.name)

        if not is_reporter and not is_assignee:
            # Name appeared in description but not in the legacy block (e.g. mentioned in a comment summary)
            skipped += 1
            continue

        current_reporter = ((fields.get("reporter") or {}).get("displayName") or "unset")
        current_assignee = ((fields.get("assignee") or {}).get("displayName") or "unset")
        summary = fields.get("summary", "")[:60]

        print(f"{key}: {summary}")
        if is_reporter:
            print(f"  reporter: '{current_reporter}' → '{args.name}'")
        if is_assignee:
            print(f"  assignee: '{current_assignee}' → '{args.name}'")

        _update_issue(site, key, account_id, is_reporter, is_assignee, args.dry_run)
        updated += 1

    action = "Would update" if args.dry_run else "Updated"
    print(f"\nDone. {action} {updated} issue(s). {skipped} skipped (name in description but not in legacy block).")


if __name__ == "__main__":
    main()
