"""
write_rest.py – Strategy B writer: create issues and comments in Workspace B.

This module is the only Strategy-B-specific layer.  The extraction and
transformation logic (extract.py, transform_rest.py) is reused unchanged.

Key responsibilities:
  1. Resolve mapped emails → accountIds in Workspace B (cached).
  2. Create issues via POST /rest/api/3/issue, in parent-before-subtask order.
  3. Resolve subtask parent keys (source key → dest key) before creating them.
  4. Post comments via POST /rest/api/3/issue/{key}/comment.
  5. Log a clear summary and skip (not abort) on per-issue errors.

Usage:
  from write_rest import write_issues_rest
  write_issues_rest(transformed_issues, site_b, dest_project_key)
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth

from config import SiteConfig


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
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
    retries: int = 3,
) -> Any:
    """Make an authenticated request with retry on 429 / transient 5xx."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(
                method, url,
                json=json,
                params=params,
                headers=_headers(),
                auth=_auth(site),
                timeout=30,
            )
        except requests.RequestException as exc:
            if attempt == retries:
                raise
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10))
            print(f"[write_rest] Rate limited – waiting {wait}s …", file=sys.stderr)
            time.sleep(wait)
            continue

        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise requests.HTTPError(
                f"HTTP {resp.status_code} {method} {url} — {detail}",
                response=resp,
            )

        # 204 No Content has no body
        if resp.status_code == 204:
            return {}
        return resp.json()

    raise RuntimeError(f"Failed {method} {url} after {retries} retries.")


# ---------------------------------------------------------------------------
# User resolution
# ---------------------------------------------------------------------------

class UserResolver:
    """
    Resolves email addresses → accountIds in Workspace B.

    Results are cached for the lifetime of the migration run.
    """

    def __init__(self, site: SiteConfig) -> None:
        self._site = site
        self._cache: Dict[str, Optional[str]] = {}

    def resolve(self, email: str) -> Optional[str]:
        """Return accountId for *email* in Workspace B, or None if not found."""
        if email in self._cache:
            return self._cache[email]

        url = f"{self._site.base_url}/rest/api/3/user/search"
        try:
            results = _request("GET", url, self._site, params={"query": email})
        except requests.HTTPError as exc:
            print(f"[write_rest] Warning: user lookup failed for '{email}': {exc}", file=sys.stderr)
            self._cache[email] = None
            return None

        account_id: Optional[str] = None
        for user in results:
            if isinstance(user, dict) and user.get("emailAddress", "").lower() == email.lower():
                account_id = user.get("accountId")
                break

        if account_id is None and results:
            # Fallback: take the first result if email field isn't returned
            # (Workspace B may hide email addresses).
            account_id = results[0].get("accountId") if isinstance(results[0], dict) else None
            if account_id:
                print(
                    f"[write_rest] Note: exact email match not found for '{email}'; "
                    f"using first result (accountId={account_id})."
                )

        if account_id is None:
            print(f"[write_rest] Warning: no Workspace B account found for '{email}'.")

        self._cache[email] = account_id
        return account_id


# ---------------------------------------------------------------------------
# Issue creation
# ---------------------------------------------------------------------------

def _resolve_user_fields(fields: Dict[str, Any], resolver: UserResolver) -> Dict[str, Any]:
    """
    Replace {"email": "..."} stubs in reporter/assignee with {"accountId": "..."}.
    Removes the field entirely if the account cannot be resolved.
    """
    out = dict(fields)
    for field_name in ("reporter", "assignee"):
        stub = out.get(field_name)
        if isinstance(stub, dict) and "email" in stub:
            account_id = resolver.resolve(stub["email"])
            if account_id:
                out[field_name] = {"accountId": account_id}
            else:
                del out[field_name]  # leave unassigned rather than erroring
    return out


def _build_issue_payload(
    item: Dict[str, Any],
    dest_project_key: str,
    key_map: Dict[str, str],
    resolver: UserResolver,
) -> Dict[str, Any]:
    """
    Build the final POST /rest/api/3/issue payload for *item*.

    Resolves user emails → accountIds and substitutes the destination parent key
    for subtasks.
    """
    fields = dict(item["fields"])

    # Inject destination project.
    fields["project"] = {"key": dest_project_key}

    # Resolve parent key for subtasks.
    source_parent = fields.pop("_source_parent_key", None)
    if source_parent:
        dest_parent = key_map.get(source_parent)
        if dest_parent:
            fields["parent"] = {"key": dest_parent}
        else:
            print(
                f"[write_rest] Warning: parent '{source_parent}' not yet in key map "
                f"for subtask '{item['source_key']}' — parent field omitted.",
                file=sys.stderr,
            )

    # Resolve user fields.
    fields = _resolve_user_fields(fields, resolver)

    return {"fields": fields}


def _create_issue(site: SiteConfig, payload: Dict[str, Any]) -> str:
    """POST a single issue; returns the new issue key in Workspace B."""
    url = f"{site.base_url}/rest/api/3/issue"
    data = _request("POST", url, site, json=payload)
    return data["key"]


def _post_comment(site: SiteConfig, dest_key: str, comment: Dict[str, Any]) -> None:
    """POST a single comment to *dest_key*."""
    url = f"{site.base_url}/rest/api/3/issue/{dest_key}/comment"
    _request("POST", url, site, json={"body": comment["body"]})


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def _verify_project(site: SiteConfig, project_key: str) -> None:
    """
    Confirm the destination project exists and is accessible.
    Raises SystemExit with a clear message if it doesn't.
    """
    url = f"{site.base_url}/rest/api/3/project/{project_key}"
    try:
        _request("GET", url, site)
        print(f"[write_rest] Verified destination project '{project_key}' exists.")
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        if status in (400, 404):
            print(
                f"\n[write_rest] ERROR: Project '{project_key}' was not found in {site.base_url}.\n"
                f"  Create the project in Workspace B first, then re-run.\n",
                file=sys.stderr,
            )
        else:
            print(
                f"\n[write_rest] ERROR: Could not verify project '{project_key}': {exc}\n",
                file=sys.stderr,
            )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main writer
# ---------------------------------------------------------------------------

def write_issues_rest(
    transformed_issues: List[Dict[str, Any]],
    site_b: SiteConfig,
    dest_project_key: str,
) -> Tuple[int, int, int]:
    """
    Create all issues and comments in Workspace B.

    Args:
      transformed_issues – output of transform_issues_rest() (parents first).
      site_b             – SiteConfig for Workspace B.
      dest_project_key   – project key in Workspace B to create issues under.

    Returns:
      (issues_created, comments_posted, errors) counts.
    """
    _verify_project(site_b, dest_project_key)

    resolver = UserResolver(site_b)
    key_map: Dict[str, str] = {}   # source_key → dest_key
    issues_created = 0
    comments_posted = 0
    errors = 0

    total = len(transformed_issues)
    print(f"[write_rest] Creating {total} issues in {site_b.base_url} "
          f"project '{dest_project_key}' …")

    for idx, item in enumerate(transformed_issues, start=1):
        source_key = item["source_key"]
        try:
            payload = _build_issue_payload(item, dest_project_key, key_map, resolver)
            dest_key = _create_issue(site_b, payload)
            key_map[source_key] = dest_key
            issues_created += 1
            print(f"[write_rest]   [{idx}/{total}] {source_key} → {dest_key}")
        except Exception as exc:
            print(
                f"[write_rest]   [{idx}/{total}] ERROR creating '{source_key}': {exc}",
                file=sys.stderr,
            )
            errors += 1
            continue

        # Post comments for this issue.
        for comment in item.get("comments", []):
            try:
                _post_comment(site_b, dest_key, comment)
                comments_posted += 1
            except Exception as exc:
                print(
                    f"[write_rest]     ERROR posting comment on '{dest_key}': {exc}",
                    file=sys.stderr,
                )
                errors += 1

    print(
        f"\n[write_rest] Done.  "
        f"{issues_created} issues created, "
        f"{comments_posted} comments posted, "
        f"{errors} error(s)."
    )
    return issues_created, comments_posted, errors
