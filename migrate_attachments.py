#!/usr/bin/env python3
"""
migrate_attachments.py – Copy issue attachments from Workspace A to Workspace B.

For each issue in the destination project, reads the "Migrated from: XXXX-NNN"
line prepended to the description to identify the source issue, then downloads
any attachments from Workspace A and uploads them to the corresponding
Workspace B issue.

Requires credentials for BOTH workspaces:
  JIRA_A_BASE_URL, JIRA_A_EMAIL, JIRA_A_API_TOKEN  (source – download)
  JIRA_B_BASE_URL, JIRA_B_EMAIL, JIRA_B_API_TOKEN  (destination – upload)

Usage:
  python migrate_attachments.py --project DESTPROJ

Options:
  --project, -p    Destination project key in Workspace B (required)
  --dry-run        Show what would be uploaded without making any changes
  --cache-dir DIR  Local directory for temporarily storing downloaded files
                   (default: /tmp/jira_attachment_cache). Files are kept after
                   the run so re-runs skip re-downloading from Workspace A.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _site_a() -> Tuple[str, HTTPBasicAuth]:
    url   = os.environ.get("JIRA_A_BASE_URL", "").rstrip("/")
    email = os.environ.get("JIRA_A_EMAIL", "")
    token = os.environ.get("JIRA_A_API_TOKEN", "")
    if not (url and email and token):
        print("ERROR: Set JIRA_A_BASE_URL, JIRA_A_EMAIL, JIRA_A_API_TOKEN.", file=sys.stderr)
        sys.exit(1)
    return url, HTTPBasicAuth(email, token)


def _site_b() -> Tuple[str, HTTPBasicAuth]:
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


def _download_file(url: str, auth: HTTPBasicAuth, dest: Path) -> int:
    """Stream *url* to *dest*. Returns file size in bytes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, auth=auth, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
    return dest.stat().st_size


def _upload_attachment(
    base_url: str, auth: HTTPBasicAuth, dest_key: str, file_path: Path
) -> None:
    """Upload a file to *dest_key* in Workspace B."""
    url = f"{base_url}/rest/api/3/issue/{dest_key}/attachments"
    # X-Atlassian-Token header is required to bypass XSRF protection for attachment uploads.
    headers = {
        "Accept": "application/json",
        "X-Atlassian-Token": "no-check",
    }
    with file_path.open("rb") as f:
        resp = requests.post(
            url,
            headers=headers,
            auth=auth,
            files={"file": (file_path.name, f)},
            timeout=60,
        )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# ADF: extract "Migrated from: XXXX-NNN"
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
    """
    Extract the source issue key from the 'Migrated from: XXXX-NNN' line
    prepended to the ADF description during migration.
    Returns None if not found.
    """
    for text in _extract_text_nodes(description):
        m = _MIGRATED_FROM_RE.search(text)
        if m:
            return m.group(1).upper()
    return None


# ---------------------------------------------------------------------------
# Workspace B: fetch issues + existing attachment names
# ---------------------------------------------------------------------------

def _fetch_dest_issues(base_url: str, auth: HTTPBasicAuth, project: str) -> List[Dict[str, Any]]:
    url = f"{base_url}/rest/api/3/search/jql"
    issues: List[Dict[str, Any]] = []
    next_page_token: Optional[str] = None

    print(f"Fetching issues from destination project '{project}' …")
    while True:
        body: Dict[str, Any] = {
            "jql": f'project = "{project}" ORDER BY created ASC',
            "maxResults": 100,
            "fields": ["summary", "description", "attachment"],
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token
        data = _post_json(url, auth, body)
        batch = data.get("issues") or []
        issues.extend(batch)
        next_page_token = data.get("nextPageToken")
        if not next_page_token or not batch:
            break

    print(f"Found {len(issues)} issue(s) in destination project.")
    return issues


def _existing_attachment_names(issue_fields: Dict[str, Any]) -> set:
    """Return the set of filenames already attached to a destination issue."""
    return {
        att["filename"]
        for att in (issue_fields.get("attachment") or [])
        if att.get("filename")
    }


# ---------------------------------------------------------------------------
# Workspace A: fetch attachment metadata for an issue
# ---------------------------------------------------------------------------

def _fetch_source_attachments(base_url: str, auth: HTTPBasicAuth, source_key: str) -> List[Dict[str, Any]]:
    url = f"{base_url}/rest/api/3/issue/{source_key}"
    try:
        data = _get(url, auth, params={"fields": "attachment"})
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            print(f"  Warning: source issue '{source_key}' not found in Workspace A — skipping.",
                  file=sys.stderr)
            return []
        raise
    return (data.get("fields") or {}).get("attachment") or []


# ---------------------------------------------------------------------------
# Main migration loop
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate issue attachments from Workspace A to Workspace B."
    )
    parser.add_argument("--project", "-p", required=True,
                        help="Destination project key in Workspace B")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be uploaded without making changes")
    parser.add_argument("--cache-dir", default="/tmp/jira_attachment_cache",
                        help="Local cache directory for downloaded files (default: /tmp/jira_attachment_cache)")
    args = parser.parse_args()

    a_url, a_auth = _site_a()
    b_url, b_auth = _site_b()
    cache_dir = Path(args.cache_dir).expanduser().resolve()

    if args.dry_run:
        print("--- DRY RUN — no files will be uploaded ---\n")

    print(f"Attachment cache: {cache_dir}\n")

    dest_issues = _fetch_dest_issues(b_url, b_auth, args.project)

    total_uploaded = 0
    total_skipped  = 0
    total_errors   = 0

    for issue in dest_issues:
        dest_key    = issue["key"]
        fields      = issue.get("fields") or {}
        description = fields.get("description")
        summary     = (fields.get("summary") or "")[:60]

        source_key = _parse_source_key(description)
        if not source_key:
            # Issue has no "Migrated from:" line — not a migrated issue, skip silently.
            continue

        source_attachments = _fetch_source_attachments(a_url, a_auth, source_key)
        if not source_attachments:
            continue

        existing = _existing_attachment_names(fields)
        new_attachments = [a for a in source_attachments if a.get("filename") not in existing]

        if not new_attachments:
            print(f"{dest_key} ← {source_key}: all {len(source_attachments)} attachment(s) already present, skipping.")
            total_skipped += len(source_attachments)
            continue

        print(f"{dest_key} ← {source_key}: {summary!r}")
        print(f"  {len(new_attachments)} to upload, {len(source_attachments) - len(new_attachments)} already present.")

        for att in new_attachments:
            filename    = att.get("filename") or att.get("id", "unknown")
            content_url = att.get("content")
            if not content_url:
                print(f"  Skipping '{filename}' — no content URL.", file=sys.stderr)
                total_skipped += 1
                continue

            cached_path = cache_dir / source_key / filename

            # Download from Workspace A (use cache if available).
            if cached_path.exists():
                print(f"  '{filename}' — using cached copy.")
            elif args.dry_run:
                print(f"  [dry-run] Would download '{filename}' from {source_key}.")
            else:
                try:
                    size = _download_file(content_url, a_auth, cached_path)
                    print(f"  Downloaded '{filename}' ({size / 1024:.1f} KB).")
                except Exception as exc:
                    print(f"  ERROR downloading '{filename}': {exc}", file=sys.stderr)
                    total_errors += 1
                    continue

            # Upload to Workspace B.
            if args.dry_run:
                print(f"  [dry-run] Would upload '{filename}' → {dest_key}.")
                total_uploaded += 1
                continue

            try:
                _upload_attachment(b_url, b_auth, dest_key, cached_path)
                print(f"  Uploaded '{filename}' → {dest_key}.")
                total_uploaded += 1
            except Exception as exc:
                print(f"  ERROR uploading '{filename}' to {dest_key}: {exc}", file=sys.stderr)
                total_errors += 1

    action = "Would upload" if args.dry_run else "Uploaded"
    print(f"\nDone. {action} {total_uploaded} attachment(s). "
          f"{total_skipped} skipped (already present). {total_errors} error(s).")


if __name__ == "__main__":
    main()
