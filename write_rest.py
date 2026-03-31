"""
write_rest.py – Strategy B writer: create issues and comments in Workspace B.

This module is the only Strategy-B-specific layer.  The extraction and
transformation logic (extract.py, transform_rest.py) is reused unchanged.

Key responsibilities:
  1. Resolve mapped emails → accountIds in Workspace B (cached).
  2. Validate destination issue types; apply issue_type_map + fallback.
  3. Create issues via POST /rest/api/3/issue, in parent-before-subtask order.
  4. Resolve subtask parent keys (source key → dest key) before creating them.
  5. Transition each issue to its source status via POST /rest/api/3/issue/{key}/transitions.
  6. Post comments via POST /rest/api/3/issue/{key}/comment.
  7. Log a clear summary and skip (not abort) on per-issue errors.

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

from config import SiteConfig, MigrationConfig


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
# Sprint resolution
# ---------------------------------------------------------------------------

class SprintResolver:
    """
    Resolves sprint names → sprint IDs in Workspace B.

    On first use it looks up the agile board for the destination project,
    fetches existing sprints, and caches them by name.  Missing sprints are
    created automatically with the same name, dates, and goal as the source.
    Sprint state (active / closed / future) is replicated via a follow-up PUT.
    """

    def __init__(self, site: SiteConfig, dest_project_key: str) -> None:
        self._site = site
        self._dest_project_key = dest_project_key
        self._board_id: Optional[int] = None
        self._sprint_cache: Dict[str, int] = {}   # name → sprint_id in Workspace B
        self._initialized = False
        self._available = False   # False if no board found or init failed

    def _ensure_init(self) -> bool:
        if self._initialized:
            return self._available
        self._initialized = True

        # Find the scrum/kanban board for the destination project.
        url = f"{self._site.base_url}/rest/agile/1.0/board"
        try:
            data = _request("GET", url, self._site, params={"projectKeyOrId": self._dest_project_key})
            boards = data.get("values") or []
            if not boards:
                print(
                    f"[write_rest] Warning: no agile board found for project "
                    f"'{self._dest_project_key}' — sprint migration skipped.",
                    file=sys.stderr,
                )
                return False
            self._board_id = boards[0]["id"]
            print(f"[write_rest] Sprint board: id={self._board_id} "
                  f"name='{boards[0].get('name')}' for project '{self._dest_project_key}'.")
        except Exception as exc:
            print(f"[write_rest] Warning: could not find agile board: {exc} — "
                  f"sprint migration skipped.", file=sys.stderr)
            return False

        # Cache all sprints that already exist on this board.
        sprint_url = f"{self._site.base_url}/rest/agile/1.0/board/{self._board_id}/sprint"
        start_at = 0
        while True:
            try:
                page = _request("GET", sprint_url, self._site,
                                params={"startAt": start_at, "maxResults": 50})
                for s in page.get("values") or []:
                    if s.get("name") and s.get("id"):
                        self._sprint_cache[s["name"]] = s["id"]
                if page.get("isLast", True):
                    break
                start_at += len(page.get("values") or [])
            except Exception as exc:
                print(f"[write_rest] Warning: could not fetch existing sprints: {exc}",
                      file=sys.stderr)
                break

        if self._sprint_cache:
            print(f"[write_rest] Found {len(self._sprint_cache)} existing sprint(s) "
                  f"in destination board.")
        self._available = True
        return True

    def resolve(self, sprint_name: str, sprint_data: Dict[str, Any]) -> Optional[int]:
        """Return the Workspace B sprint ID for *sprint_name*, creating it if needed."""
        if not self._ensure_init():
            return None
        if sprint_name in self._sprint_cache:
            return self._sprint_cache[sprint_name]
        return self._create_sprint(sprint_name, sprint_data)

    def _create_sprint(self, sprint_name: str, sprint_data: Dict[str, Any]) -> Optional[int]:
        url = f"{self._site.base_url}/rest/agile/1.0/sprint"
        body: Dict[str, Any] = {"name": sprint_name, "originBoardId": self._board_id}
        if sprint_data.get("startDate"):
            body["startDate"] = sprint_data["startDate"]
        if sprint_data.get("endDate"):
            body["endDate"] = sprint_data["endDate"]
        if sprint_data.get("goal"):
            body["goal"] = sprint_data["goal"]
        try:
            result = _request("POST", url, self._site, json=body)
            sprint_id = result.get("id")
            if not sprint_id:
                return None
            self._sprint_cache[sprint_name] = sprint_id
            print(f"[write_rest] Created sprint '{sprint_name}' (id={sprint_id}).")

            # Replicate sprint state.
            state = sprint_data.get("state")
            if state in ("active", "closed"):
                self._transition_sprint(sprint_id, state, sprint_data)

            return sprint_id
        except Exception as exc:
            print(f"[write_rest] Warning: could not create sprint '{sprint_name}': {exc}",
                  file=sys.stderr)
            return None

    def _transition_sprint(
        self, sprint_id: int, state: str, sprint_data: Dict[str, Any]
    ) -> None:
        url = f"{self._site.base_url}/rest/agile/1.0/sprint/{sprint_id}"
        body: Dict[str, Any] = {"state": state}
        if sprint_data.get("startDate"):
            body["startDate"] = sprint_data["startDate"]
        if state == "closed" and sprint_data.get("completeDate"):
            body["completeDate"] = sprint_data["completeDate"]
        elif sprint_data.get("endDate"):
            body["endDate"] = sprint_data["endDate"]
        try:
            _request("PUT", url, self._site, json=body)
        except Exception as exc:
            print(
                f"[write_rest] Warning: could not transition sprint {sprint_id} "
                f"to '{state}': {exc}",
                file=sys.stderr,
            )


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
    available_types: Dict[str, str],
    cfg: MigrationConfig,
    sprint_resolver: Optional["SprintResolver"] = None,
) -> Dict[str, Any]:
    """
    Build the final POST /rest/api/3/issue payload for *item*.

    Resolves user emails → accountIds and substitutes the destination parent key
    for subtasks.
    """
    fields = dict(item["fields"])

    # Inject destination project.
    fields["project"] = {"key": dest_project_key}

    # Resolve issue type to one that exists in Workspace B.
    source_type = (fields.get("issuetype") or {}).get("name") or "Task"
    fields["issuetype"] = {"name": _resolve_issue_type(source_type, available_types, cfg)}

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

    # Resolve sprint.
    source_sprint = item.get("source_sprint")
    if source_sprint and sprint_resolver:
        sprint_id = sprint_resolver.resolve(source_sprint["name"], source_sprint)
        if sprint_id:
            fields[cfg.sprint_field] = sprint_id

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


def _apply_transition(site: SiteConfig, dest_key: str, target_status: str) -> bool:
    """
    Transition *dest_key* to *target_status* by name (case-insensitive).

    Fetches the available transitions for the issue and executes the first one
    whose destination status name matches. Returns True if transitioned, False
    if no matching transition was found (e.g. the workflow in Workspace B doesn't
    have that status, or the issue is already there).
    """
    if not target_status:
        return False

    url = f"{site.base_url}/rest/api/3/issue/{dest_key}/transitions"
    data = _request("GET", url, site)
    target_lower = target_status.lower()

    for t in data.get("transitions", []):
        to_status = (t.get("to") or {}).get("name") or ""
        if to_status.lower() == target_lower:
            _request("POST", url, site, json={"transition": {"id": t["id"]}})
            return True

    return False


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def _fetch_project_issue_types(site: SiteConfig, project_key: str) -> Dict[str, Any]:
    """
    Return a dict of {type_name_lower: type_name_original} for all issue types
    available in the destination project.
    """
    url = f"{site.base_url}/rest/api/3/project/{project_key}"
    data = _request("GET", url, site)
    types: Dict[str, str] = {}
    for it in data.get("issueTypes") or []:
        name = it.get("name") or ""
        types[name.lower()] = name
    return types


def _resolve_issue_type(
    source_type: str,
    available_types: Dict[str, str],
    cfg: MigrationConfig,
) -> str:
    """
    Resolve a source issue-type name to one that exists in Workspace B.

    Resolution order:
      1. Explicit mapping in cfg.issue_type_map
      2. Exact match (case-insensitive) in available_types
      3. cfg.fallback_issue_type
    """
    # 1. Explicit override
    mapped = cfg.issue_type_map.get(source_type) or cfg.issue_type_map.get(source_type.lower())
    if mapped:
        return mapped

    # 2. Case-insensitive match
    original = available_types.get(source_type.lower())
    if original:
        return original

    # 3. Fallback
    print(
        f"[write_rest] Warning: issue type '{source_type}' not found in Workspace B; "
        f"falling back to '{cfg.fallback_issue_type}'.",
        file=sys.stderr,
    )
    return cfg.fallback_issue_type


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
    cfg: Optional[MigrationConfig] = None,
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
    from config import MigrationConfig as _MC
    if cfg is None:
        cfg = _MC(jira_a=site_b, jira_b=None)  # minimal fallback; callers should always pass cfg

    _verify_project(site_b, dest_project_key)
    available_types = _fetch_project_issue_types(site_b, dest_project_key)
    print(f"[write_rest] Issue types available in '{dest_project_key}': "
          f"{', '.join(sorted(available_types.values()))}")

    resolver = UserResolver(site_b)
    sprint_resolver = SprintResolver(site_b, dest_project_key)
    key_map: Dict[str, str] = {}   # source_key → dest_key
    issues_created = 0
    comments_posted = 0
    transitions_applied = 0
    errors = 0

    total = len(transformed_issues)
    print(f"[write_rest] Creating {total} issues in {site_b.base_url} "
          f"project '{dest_project_key}' …")

    for idx, item in enumerate(transformed_issues, start=1):
        source_key = item["source_key"]
        try:
            payload = _build_issue_payload(item, dest_project_key, key_map, resolver, available_types, cfg, sprint_resolver)
            dest_key = _create_issue(site_b, payload)
            key_map[source_key] = dest_key
            issues_created += 1

            # Transition to source status.
            source_status = item.get("source_status", "")
            if source_status:
                transitioned = _apply_transition(site_b, dest_key, source_status)
                if transitioned:
                    transitions_applied += 1
                else:
                    print(f"[write_rest]     Note: no transition to '{source_status}' "
                          f"found for {dest_key} — left in default status.")

            print(f"[write_rest]   [{idx}/{total}] {source_key} → {dest_key} "
                  f"(status: {source_status or 'default'})")
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
        f"{transitions_applied} statuses applied, "
        f"{comments_posted} comments posted, "
        f"{errors} error(s)."
    )
    return issues_created, comments_posted, errors
