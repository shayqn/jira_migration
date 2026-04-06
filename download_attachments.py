#!/usr/bin/env python3
"""
download_attachments.py – Download attachments from a Jira issue (or project).

Uses Workspace A credentials to fetch attachment metadata and download files.

Usage (single issue):
  python download_attachments.py --issue-key KSW-37 --dir ~/Downloads/attachments

Usage (entire project):
  python download_attachments.py --project KSW --dir ~/Downloads/attachments

Files are saved to: <dir>/<issue-key>/<filename>
If a file already exists at that path it is skipped.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from requests.auth import HTTPBasicAuth


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _make_auth() -> HTTPBasicAuth:
    url   = os.environ.get("JIRA_A_BASE_URL", "").rstrip("/")
    email = os.environ.get("JIRA_A_EMAIL", "")
    token = os.environ.get("JIRA_A_API_TOKEN", "")
    if not (url and email and token):
        print(
            "ERROR: Set JIRA_A_BASE_URL, JIRA_A_EMAIL, and JIRA_A_API_TOKEN "
            "environment variables before running this script.",
            file=sys.stderr,
        )
        sys.exit(1)
    return HTTPBasicAuth(email, token)


def _base_url() -> str:
    return os.environ.get("JIRA_A_BASE_URL", "").rstrip("/")


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
            print(f"Rate limited – waiting {wait}s …", file=sys.stderr)
            time.sleep(wait)
            continue

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError(f"Failed GET {url} after 3 retries.")


def _download(url: str, auth: HTTPBasicAuth, dest: Path) -> int:
    """Stream *url* to *dest*. Returns file size in bytes."""
    with requests.get(url, auth=auth, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
    return dest.stat().st_size


# ---------------------------------------------------------------------------
# Attachment fetching
# ---------------------------------------------------------------------------

def _get_attachments(issue_key: str, auth: HTTPBasicAuth) -> List[Dict[str, Any]]:
    """Return the list of attachment objects for a single issue."""
    url = f"{_base_url()}/rest/api/3/issue/{issue_key}"
    data = _get(url, auth, params={"fields": "attachment,summary"})
    attachments = (data.get("fields") or {}).get("attachment") or []
    summary = (data.get("fields") or {}).get("summary", "")
    print(f"{issue_key}: {summary!r} — {len(attachments)} attachment(s)")
    return attachments


def _get_all_issue_keys(project: str, auth: HTTPBasicAuth) -> List[str]:
    """Return all issue keys for *project*, paginated."""
    url = f"{_base_url()}/rest/api/3/search/jql"
    keys: List[str] = []
    next_page_token: Optional[str] = None

    print(f"Fetching issue list for project '{project}' …")
    while True:
        body: Dict[str, Any] = {
            "jql": f'project = "{project}" ORDER BY created ASC',
            "maxResults": 100,
            "fields": ["summary"],
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        resp = requests.post(
            url, json=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            auth=auth, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        for issue in data.get("issues") or []:
            keys.append(issue["key"])

        next_page_token = data.get("nextPageToken")
        if not next_page_token or not data.get("issues"):
            break

    print(f"Found {len(keys)} issue(s).")
    return keys


# ---------------------------------------------------------------------------
# Download logic
# ---------------------------------------------------------------------------

def download_issue_attachments(
    issue_key: str,
    auth: HTTPBasicAuth,
    base_dir: Path,
) -> tuple[int, int]:
    """
    Download all attachments for *issue_key* into <base_dir>/<issue_key>/.
    Returns (downloaded, skipped) counts.
    """
    attachments = _get_attachments(issue_key, auth)
    downloaded = 0
    skipped = 0

    for att in attachments:
        filename = att.get("filename") or att.get("id", "unknown")
        content_url = att.get("content")
        if not content_url:
            print(f"  Skipping '{filename}' — no content URL", file=sys.stderr)
            skipped += 1
            continue

        dest = base_dir / issue_key / filename

        if dest.exists():
            print(f"  Skipping '{filename}' — already exists at {dest}")
            skipped += 1
            continue

        try:
            size = _download(content_url, auth, dest)
            size_kb = size / 1024
            print(f"  Downloaded '{filename}' ({size_kb:.1f} KB) → {dest}")
            downloaded += 1
        except Exception as exc:
            print(f"  ERROR downloading '{filename}': {exc}", file=sys.stderr)
            skipped += 1

    return downloaded, skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download attachments from a Jira issue or project."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--issue-key", help="Single issue key, e.g. KSW-37")
    group.add_argument("--project", "-p", help="Download all attachments from a project")
    parser.add_argument(
        "--dir", "-d", required=True,
        help="Directory to save attachments into (created if it doesn't exist)",
    )
    args = parser.parse_args()

    auth = _make_auth()
    base_dir = Path(args.dir).expanduser().resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    print(f"Saving to: {base_dir}\n")

    total_downloaded = 0
    total_skipped = 0

    if args.issue_key:
        downloaded, skipped = download_issue_attachments(args.issue_key, auth, base_dir)
        total_downloaded += downloaded
        total_skipped += skipped
    else:
        issue_keys = _get_all_issue_keys(args.project, auth)
        for key in issue_keys:
            try:
                downloaded, skipped = download_issue_attachments(key, auth, base_dir)
                total_downloaded += downloaded
                total_skipped += skipped
            except Exception as exc:
                print(f"ERROR processing {key}: {exc}", file=sys.stderr)

    print(f"\nDone. {total_downloaded} file(s) downloaded, {total_skipped} skipped.")


if __name__ == "__main__":
    main()
