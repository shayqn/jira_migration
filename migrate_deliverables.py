#!/usr/bin/env python3
"""
migrate_deliverables.py – Append 'Deliverables' content from Workspace A into
issue descriptions in Workspace B.

For each issue in the destination project, reads the "Migrated from: XXXX-NNN"
line to identify the source issue, fetches customfield_10712 (Deliverables) from
Workspace A, and appends it to the description in Workspace B — preceded by a
horizontal rule and a bold "Deliverables" heading.

Issues where the source has no Deliverables content, or where a Deliverables
block has already been appended, are skipped.

Requires credentials for BOTH workspaces:
  JIRA_A_BASE_URL, JIRA_A_EMAIL, JIRA_A_API_TOKEN  (source)
  JIRA_B_BASE_URL, JIRA_B_EMAIL, JIRA_B_API_TOKEN  (destination)

Usage:
  python migrate_deliverables.py --project DESTPROJ [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

import requests
from requests.auth import HTTPBasicAuth


DELIVERABLES_FIELD = "customfield_10712"
DELIVERABLES_SENTINEL = "Deliverables (migrated from source Jira)"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _site_a():
    url   = os.environ.get("JIRA_A_BASE_URL", "").rstrip("/")
    email = os.environ.get("JIRA_A_EMAIL", "")
    token = os.environ.get("JIRA_A_API_TOKEN", "")
    if not (url and email and token):
        print("ERROR: Set JIRA_A_BASE_URL, JIRA_A_EMAIL, JIRA_A_API_TOKEN.", file=sys.stderr)
        sys.exit(1)
    return url, HTTPBasicAuth(email, token)


def _site_b():
    url   = os.environ.get("JIRA_B_BASE_URL", "").rstrip("/")
    email = os.environ.get("JIRA_B_EMAIL", "")
    token = os.environ.get("JIRA_B_API_TOKEN", "")
    if not (url and email and token):
        print("ERROR: Set JIRA_B_BASE_URL, JIRA_B_EMAIL, JIRA_B_API_TOKEN.", file=sys.stderr)
        sys.exit(1)
    return url, HTTPBasicAuth(email, token)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, auth: HTTPBasicAuth, params: Dict[str, Any] | None = None) -> Any:
    for attempt in range(1, 4):
        try:
            resp = requests.get(
                url, params=params,
                headers={"Accept": "application/json"},
                auth=auth, timeout=30,
            )
        except requests.RequestException as exc:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10))
            print(f"  Rate limited – waiting {wait}s …", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Failed GET {url}")


def _post_json(url: str, auth: HTTPBasicAuth, body: Dict[str, Any]) -> Any:
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                url, json=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                auth=auth, timeout=30,
            )
        except requests.RequestException as exc:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10))
            print(f"  Rate limited – waiting {wait}s …", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Failed POST {url}")


def _put_json(url: str, auth: HTTPBasicAuth, body: Dict[str, Any]) -> None:
    for attempt in range(1, 4):
        try:
            resp = requests.put(
                url, json=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                auth=auth, timeout=30,
            )
        except requests.RequestException as exc:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10))
            print(f"  Rate limited – waiting {wait}s …", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return
    raise RuntimeError(f"Failed PUT {url}")


# ---------------------------------------------------------------------------
# ADF helpers
# ---------------------------------------------------------------------------

_MIGRATED_FROM_RE = re.compile(r"Migrated from:\s*([A-Z][A-Z0-9_]+-\d+)", re.IGNORECASE)


def _extract_text_nodes(node: Any) -> List[str]:
    if not isinstance(node, dict):
        return []
    if node.get("type") == "text":
        text = node.get("text", "").strip()
        return [text] if text else []
    lines = []
    for child in node.get("content") or []:
        lines.extend(_extract_text_nodes(child))
    return lines


def _parse_source_key(description: Any) -> Optional[str]:
    for text in _extract_text_nodes(description):
        m = _MIGRATED_FROM_RE.search(text)
        if m:
            return m.group(1).upper()
    return None


def _already_migrated(description: Any) -> bool:
    """Return True if the description already contains the Deliverables sentinel text."""
    return any(DELIVERABLES_SENTINEL in t for t in _extract_text_nodes(description))


def _adf_rule() -> Dict[str, Any]:
    return {"type": "rule"}


def _adf_heading(text: str, level: int = 3) -> Dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def _adf_italic(text: str) -> Dict[str, Any]:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": text, "marks": [{"type": "em"}]}],
    }


def _append_deliverables(description: Any, deliverables_adf: Any) -> Dict[str, Any]:
    """
    Return a copy of *description* with a Deliverables section appended.
    *deliverables_adf* is the raw ADF doc from customfield_10712.
    """
    if not isinstance(description, dict) or description.get("type") != "doc":
        description = {"version": 1, "type": "doc", "content": []}

    # Extract the content nodes from the deliverables ADF doc.
    deliverables_content = []
    if isinstance(deliverables_adf, dict) and deliverables_adf.get("type") == "doc":
        deliverables_content = deliverables_adf.get("content") or []

    appended = [
        _adf_rule(),
        _adf_heading(DELIVERABLES_SENTINEL),
    ] + deliverables_content

    return {
        **description,
        "content": list(description.get("content") or []) + appended,
    }


# ---------------------------------------------------------------------------
# Workspace B: fetch all issues
# ---------------------------------------------------------------------------

def _fetch_dest_issues(b_url: str, b_auth: HTTPBasicAuth, project: str) -> List[Dict[str, Any]]:
    url = f"{b_url}/rest/api/3/search/jql"
    issues: List[Dict[str, Any]] = []
    next_page_token: Optional[str] = None

    print(f"Fetching issues from destination project '{project}' …")
    while True:
        body: Dict[str, Any] = {
            "jql": f'project = "{project}" ORDER BY created ASC',
            "maxResults": 100,
            "fields": ["summary", "description"],
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token
        data = _post_json(url, b_auth, body)
        batch = data.get("issues") or []
        issues.extend(batch)
        next_page_token = data.get("nextPageToken")
        if not next_page_token or not batch:
            break

    print(f"Found {len(issues)} issue(s).")
    return issues


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Append Deliverables field content from Workspace A into Workspace B issue descriptions."
    )
    parser.add_argument("--project", "-p", required=True,
                        help="Destination project key in Workspace B")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be changed without making any updates")
    args = parser.parse_args()

    a_url, a_auth = _site_a()
    b_url, b_auth = _site_b()

    if args.dry_run:
        print("--- DRY RUN — no changes will be made ---\n")

    dest_issues = _fetch_dest_issues(b_url, b_auth, args.project)

    updated  = 0
    skipped  = 0
    no_data  = 0
    errors   = 0

    for issue in dest_issues:
        dest_key    = issue["key"]
        fields      = issue.get("fields") or {}
        description = fields.get("description")
        summary     = (fields.get("summary") or "")[:60]

        source_key = _parse_source_key(description)
        if not source_key:
            skipped += 1
            continue

        if _already_migrated(description):
            print(f"{dest_key} ← {source_key}: Deliverables already present, skipping.")
            skipped += 1
            continue

        # Fetch Deliverables from Workspace A.
        try:
            source_data = _get(
                f"{a_url}/rest/api/3/issue/{source_key}",
                a_auth,
                params={"fields": DELIVERABLES_FIELD},
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            if status == 404:
                print(f"{dest_key} ← {source_key}: source issue not found, skipping.",
                      file=sys.stderr)
            else:
                print(f"{dest_key} ← {source_key}: ERROR fetching source: {exc}",
                      file=sys.stderr)
                errors += 1
            skipped += 1
            continue

        deliverables_adf = (source_data.get("fields") or {}).get(DELIVERABLES_FIELD)
        if not deliverables_adf:
            no_data += 1
            continue

        print(f"{dest_key} ← {source_key}: {summary!r} — appending Deliverables.")

        if args.dry_run:
            updated += 1
            continue

        new_description = _append_deliverables(description, deliverables_adf)
        try:
            _put_json(
                f"{b_url}/rest/api/3/issue/{dest_key}",
                b_auth,
                {"fields": {"description": new_description}},
            )
            updated += 1
        except Exception as exc:
            print(f"  ERROR updating {dest_key}: {exc}", file=sys.stderr)
            errors += 1

    action = "Would update" if args.dry_run else "Updated"
    print(
        f"\nDone. {action} {updated} issue(s). "
        f"{skipped} skipped (no source key or already done). "
        f"{no_data} had no Deliverables content. "
        f"{errors} error(s)."
    )


if __name__ == "__main__":
    main()
