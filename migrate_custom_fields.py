#!/usr/bin/env python3
"""
migrate_custom_fields.py – Migrate custom fields from Workspace A to Workspace B
using a JSON config file that defines the field mappings.

For each issue in the destination project, reads the "Migrated from: XXXX-NNN"
line to identify the source issue, fetches the configured fields from Workspace A,
and writes them to Workspace B.

Supported field types:
  number      — numeric value, copied directly
  text        — plain string, copied directly
  date        — date string (YYYY-MM-DD), copied directly
  url         — URL string, copied directly
  select      — single select/dropdown, value copied by name
  adf         — ADF rich text, requires a matching dest_field in Workspace B
  adf_append  — ADF rich text or plain value with no dest field; appended to
                description under a heading
  people      — array of user objects; accountIds resolved via user_mapping.csv;
                unmapped users appended to description as a fallback

Requires credentials for BOTH workspaces:
  JIRA_A_BASE_URL, JIRA_A_EMAIL, JIRA_A_API_TOKEN  (source)
  JIRA_B_BASE_URL, JIRA_B_EMAIL, JIRA_B_API_TOKEN  (destination)

Usage:
  python migrate_custom_fields.py --config field_migration_config.HEG.json [--dry-run]
  python migrate_custom_fields.py --config field_migration_config.HEG.json [--mapping path/to/user_mapping.csv]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth

from user_mapping import load_user_mapping


UNMAPPED_PEOPLE_SENTINEL = "Unmapped {field_name} (from source Jira):"


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


def _has_sentinel(description: Any, sentinel: str) -> bool:
    return any(sentinel in t for t in _extract_text_nodes(description))


def _italic_paragraph(text: str) -> Dict[str, Any]:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": text, "marks": [{"type": "em"}]}],
    }


def _adf_heading(text: str, level: int = 3) -> Dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def _adf_rule() -> Dict[str, Any]:
    return {"type": "rule"}


def _ensure_doc(description: Any) -> Dict[str, Any]:
    if not isinstance(description, dict) or description.get("type") != "doc":
        return {"version": 1, "type": "doc", "content": []}
    return description


def _append_to_description(
    description: Any,
    heading: str,
    content_nodes: List[Dict[str, Any]],
) -> Dict[str, Any]:
    description = _ensure_doc(description)
    appended = [_adf_rule(), _adf_heading(heading)] + content_nodes
    return {**description, "content": list(description.get("content") or []) + appended}


def _value_to_adf_nodes(value: Any, field_name: str) -> List[Dict[str, Any]]:
    """Convert a field value to a list of ADF content nodes for appending."""
    if isinstance(value, dict) and value.get("type") == "doc":
        return value.get("content") or []
    # Plain value (date, url, text, number) — render as a paragraph
    return [{"type": "paragraph", "content": [{"type": "text", "text": str(value)}]}]


# ---------------------------------------------------------------------------
# People field resolution
# ---------------------------------------------------------------------------

class UserResolver:
    """Resolves source accountIds/emails → Workspace B accountIds, with caching."""

    def __init__(self, b_url: str, b_auth: HTTPBasicAuth, mapping: Dict[str, str]) -> None:
        self._b_url = b_url
        self._b_auth = b_auth
        self._mapping = mapping
        self._cache: Dict[str, Optional[str]] = {}

    def resolve_obj(self, user_obj: Dict[str, Any]) -> Optional[str]:
        source_email      = user_obj.get("emailAddress") or ""
        source_account_id = user_obj.get("accountId") or ""
        target_email = (
            self._mapping.get(source_email)
            or self._mapping.get(source_account_id)
        )
        if not target_email:
            return None
        return self._lookup_b_account(target_email)

    def _lookup_b_account(self, target_email: str) -> Optional[str]:
        if target_email in self._cache:
            return self._cache[target_email]
        url = f"{self._b_url}/rest/api/3/user/search"
        try:
            results = _get(url, self._b_auth, params={"query": target_email})
            for user in results:
                if isinstance(user, dict) and \
                        user.get("emailAddress", "").lower() == target_email.lower():
                    self._cache[target_email] = user.get("accountId")
                    return self._cache[target_email]
            if results and isinstance(results[0], dict):
                self._cache[target_email] = results[0].get("accountId")
                return self._cache[target_email]
        except Exception as exc:
            print(f"  Warning: could not look up '{target_email}' in Workspace B: {exc}",
                  file=sys.stderr)
        self._cache[target_email] = None
        return None


# ---------------------------------------------------------------------------
# Per-field value processing
# ---------------------------------------------------------------------------

def _process_field(
    field_cfg: Dict[str, Any],
    source_value: Any,
    dest_current_value: Any,
    description: Any,
    user_resolver: UserResolver,
) -> Tuple[Optional[Any], Any]:
    """
    Process a single field value.

    Returns:
      (dest_field_value, updated_description)
      dest_field_value is None if nothing should be written to the dest field.
    """
    field_type  = field_cfg["type"]
    field_name  = field_cfg["name"]
    dest_field  = field_cfg.get("dest_field")

    # Skip if already populated in Workspace B
    if dest_current_value is not None and dest_current_value != [] and dest_current_value != "":
        print(f"  {field_name}: already set, skipping.")
        return None, description

    if field_type in ("number", "text", "date", "url"):
        if dest_field:
            print(f"  {field_name}: {source_value!r}")
            return source_value, description
        # No dest field — append to description
        sentinel = f"{field_name} (from source Jira)"
        if not _has_sentinel(description, sentinel):
            description = _append_to_description(
                description, sentinel,
                _value_to_adf_nodes(source_value, field_name),
            )
            print(f"  {field_name}: appended to description ({source_value!r})")
        return None, description

    if field_type == "select":
        option_name = source_value.get("value") if isinstance(source_value, dict) else str(source_value)
        if dest_field:
            print(f"  {field_name}: {option_name!r}")
            return {"value": option_name}, description
        sentinel = f"{field_name} (from source Jira)"
        if not _has_sentinel(description, sentinel):
            description = _append_to_description(
                description, sentinel,
                [{"type": "paragraph", "content": [{"type": "text", "text": option_name}]}],
            )
            print(f"  {field_name}: appended to description ({option_name!r})")
        return None, description

    if field_type == "adf":
        if dest_field:
            print(f"  {field_name}: copying ADF content.")
            return source_value, description
        # Fall through to adf_append behaviour
        field_type = "adf_append"

    if field_type == "adf_append":
        sentinel = f"{field_name} (from source Jira)"
        if not _has_sentinel(description, sentinel):
            description = _append_to_description(
                description, sentinel,
                _value_to_adf_nodes(source_value, field_name),
            )
            print(f"  {field_name}: appended to description.")
        else:
            print(f"  {field_name}: already appended, skipping.")
        return None, description

    if field_type == "people":
        if not isinstance(source_value, list):
            source_value = [source_value] if source_value else []
        mapped_ids   = []
        unmapped_names = []
        for obj in source_value:
            if not isinstance(obj, dict):
                continue
            b_account_id = user_resolver.resolve_obj(obj)
            if b_account_id:
                mapped_ids.append(b_account_id)
            else:
                display = obj.get("displayName") or obj.get("emailAddress") or "Unknown"
                unmapped_names.append(display)

        print(f"  {field_name}: {len(mapped_ids)} mapped, {len(unmapped_names)} unmapped.")

        if unmapped_names:
            sentinel = f"Unmapped {field_name} (from source Jira):"
            if not _has_sentinel(description, sentinel):
                description = _append_to_description(
                    description, sentinel,
                    [_italic_paragraph(", ".join(unmapped_names))],
                )

        if mapped_ids and dest_field:
            return [{"accountId": aid} for aid in mapped_ids], description
        return None, description

    print(f"  Warning: unknown field type '{field_type}' for '{field_name}', skipping.",
          file=sys.stderr)
    return None, description


# ---------------------------------------------------------------------------
# Fetch destination issues
# ---------------------------------------------------------------------------

def _fetch_dest_issues(
    b_url: str, b_auth: HTTPBasicAuth, project: str, dest_fields: List[str]
) -> List[Dict[str, Any]]:
    url = f"{b_url}/rest/api/3/search/jql"
    issues: List[Dict[str, Any]] = []
    next_page_token: Optional[str] = None
    requested = ["summary", "description"] + dest_fields

    print(f"Fetching issues from destination project '{project}' …")
    while True:
        body: Dict[str, Any] = {
            "jql": f'project = "{project}" ORDER BY created ASC',
            "maxResults": 100,
            "fields": requested,
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
        description="Migrate custom fields from Workspace A to Workspace B using a JSON config."
    )
    parser.add_argument("--config", "-c", required=True,
                        help="Path to field migration config JSON, e.g. field_migration_config.HEG.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be changed without making any updates")
    parser.add_argument("--mapping", "-m",
                        default=str(Path(__file__).parent / "user_mapping.csv"),
                        help="Path to user_mapping.csv (default: user_mapping.csv next to script)")
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: Config file '{config_path}' not found.", file=sys.stderr)
        sys.exit(1)
    with config_path.open() as f:
        config = json.load(f)

    dest_project = config.get("dest_project")
    if not dest_project:
        print("ERROR: Config must have a 'dest_project' key.", file=sys.stderr)
        sys.exit(1)

    field_configs: List[Dict[str, Any]] = config.get("fields") or []
    if not field_configs:
        print("ERROR: Config has no 'fields' entries.", file=sys.stderr)
        sys.exit(1)

    a_url, a_auth = _site_a()
    b_url, b_auth = _site_b()
    email_to_target = load_user_mapping(args.mapping)
    user_resolver = UserResolver(b_url, b_auth, email_to_target)

    if args.dry_run:
        print("--- DRY RUN — no changes will be made ---\n")

    # Collect source and dest field IDs
    source_fields = [fc["source_field"] for fc in field_configs]
    dest_fields   = [fc["dest_field"] for fc in field_configs if fc.get("dest_field")]

    print(f"Migrating {len(field_configs)} field(s) to project '{dest_project}':")
    for fc in field_configs:
        print(f"  {fc['name']} ({fc['type']}): {fc['source_field']} → {fc.get('dest_field') or 'description append'}")
    print()

    dest_issues = _fetch_dest_issues(b_url, b_auth, dest_project, dest_fields)

    updated = 0
    skipped = 0
    errors  = 0

    for issue in dest_issues:
        dest_key    = issue["key"]
        fields      = issue.get("fields") or {}
        description = fields.get("description")
        summary     = (fields.get("summary") or "")[:60]

        source_key = _parse_source_key(description)
        if not source_key:
            skipped += 1
            continue

        # Fetch all source fields in one API call
        try:
            source_data = _get(
                f"{a_url}/rest/api/3/issue/{source_key}",
                a_auth,
                params={"fields": ",".join(source_fields)},
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            if status == 404:
                print(f"{dest_key} ← {source_key}: source not found, skipping.", file=sys.stderr)
            else:
                print(f"{dest_key} ← {source_key}: ERROR fetching source (HTTP {status}): {exc}",
                      file=sys.stderr)
                errors += 1
            skipped += 1
            continue

        source_fields_data = source_data.get("fields") or {}

        # Check if any source fields have data worth processing
        has_data = any(
            source_fields_data.get(fc["source_field"]) is not None
            for fc in field_configs
        )
        if not has_data:
            skipped += 1
            continue

        print(f"{dest_key} ← {source_key}: {summary!r}")

        fields_payload: Dict[str, Any] = {}
        new_description = description

        for fc in field_configs:
            source_value = source_fields_data.get(fc["source_field"])
            if source_value is None:
                continue

            dest_field         = fc.get("dest_field")
            dest_current_value = fields.get(dest_field) if dest_field else None

            dest_value, new_description = _process_field(
                fc, source_value, dest_current_value, new_description, user_resolver
            )

            if dest_value is not None and dest_field:
                fields_payload[dest_field] = dest_value

        if new_description is not description:
            fields_payload["description"] = new_description

        if not fields_payload:
            skipped += 1
            continue

        if args.dry_run:
            updated += 1
            continue

        try:
            _put_json(
                f"{b_url}/rest/api/3/issue/{dest_key}",
                b_auth,
                {"fields": fields_payload},
            )
            updated += 1
        except Exception as exc:
            print(f"  ERROR updating {dest_key}: {exc}", file=sys.stderr)
            errors += 1

    action = "Would update" if args.dry_run else "Updated"
    print(f"\nDone. {action} {updated} issue(s). {skipped} skipped. {errors} error(s).")


if __name__ == "__main__":
    main()
