"""
transform_rest.py – Transform raw Jira issues into Workspace B REST API payloads.

Key differences from transform.py (Strategy A / CSV):
  - Description is passed through as raw ADF, preserving all rich-text structure.
  - Comments are included, each wrapped with an italic author/date header in ADF.
  - User fields carry the mapped *email* (not accountId); write_rest.py resolves
    these to accountIds via the Workspace B user-search API at write time.
  - Issues are flagged as subtasks so write_rest.py can order creation correctly
    (parents must exist before their subtasks are created).

Output shape per issue (a plain dict):
  {
    "source_key":        str,          # e.g. "PROJ1-42" — for logging / key mapping
    "source_parent_key": str | None,   # set for subtasks; used to order creation
    "is_subtask":        bool,
    "fields": {                        # body of POST /rest/api/3/issue {"fields": ...}
      "summary":     str,
      "description": dict | None,      # ADF document or None
      "issuetype":   {"name": str},
      "priority":    {"name": str} | None,
      "labels":      [str, ...],
      "components":  [{"name": str}, ...],
      "reporter":    {"email": str} | None,   # resolved to accountId by write_rest.py
      "assignee":    {"email": str} | None,
      "parent":      {"key": str} | None,     # dest key filled in by write_rest.py
    },
    "legacy_reporter": str,   # source identity if unmapped, else ""
    "legacy_assignee": str,
    "comments": [             # ordered oldest-first
      {
        "body":           dict,   # ADF document (author header prepended)
        "source_author":  str,    # display name or email
        "source_created": str,    # ISO timestamp
      },
      ...
    ],
  }
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import MigrationConfig
from user_mapping import resolve_user


# ---------------------------------------------------------------------------
# ADF helpers
# ---------------------------------------------------------------------------

def _empty_doc() -> Dict[str, Any]:
    return {"version": 1, "type": "doc", "content": []}


def _italic_paragraph(text: str) -> Dict[str, Any]:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": text, "marks": [{"type": "em"}]}],
    }


def _rule_node() -> Dict[str, Any]:
    return {"type": "rule"}


def _prepend_to_doc(doc: Any, *prepend_nodes: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *doc* (ADF) with *prepend_nodes* inserted at the top."""
    if not isinstance(doc, dict) or doc.get("type") != "doc":
        doc = _empty_doc()
    return {
        **doc,
        "content": list(prepend_nodes) + list(doc.get("content") or []),
    }


def _append_legacy_block_adf(doc: Any, reporter_legacy: str, assignee_legacy: str) -> Dict[str, Any]:
    """Append a legacy-user block to an ADF document."""
    if not isinstance(doc, dict) or doc.get("type") != "doc":
        doc = _empty_doc()
    extra: List[Dict[str, Any]] = [_rule_node(), _italic_paragraph("Legacy user info (from source Jira):")]
    if reporter_legacy:
        extra.append(_italic_paragraph(f"Original reporter: {reporter_legacy}"))
    if assignee_legacy:
        extra.append(_italic_paragraph(f"Original assignee: {assignee_legacy}"))
    return {**doc, "content": list(doc.get("content") or []) + extra}


# ---------------------------------------------------------------------------
# User helpers  (mirrors transform.py but returns email dicts, not plain strings)
# ---------------------------------------------------------------------------

def _user_email(obj: Any) -> Optional[str]:
    if not isinstance(obj, dict):
        return None
    return obj.get("emailAddress") or None


def _user_legacy_id(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    return obj.get("emailAddress") or obj.get("displayName") or obj.get("accountId") or ""


# ---------------------------------------------------------------------------
# Comment transform
# ---------------------------------------------------------------------------

def _transform_comment(comment: Dict[str, Any]) -> Dict[str, Any]:
    """
    Wrap a raw Jira comment for Strategy B.

    The comment body is ADF; we prepend an italic header line so the original
    author and timestamp are visible in Workspace B (since comments are always
    posted under the API-token user's identity).
    """
    author_obj = comment.get("author") or {}
    author_name = (
        author_obj.get("displayName")
        or author_obj.get("emailAddress")
        or author_obj.get("accountId")
        or "Unknown"
    )
    created = comment.get("created") or ""

    header_text = f"Originally posted by {author_name} on {created}:"
    raw_body = comment.get("body")

    if not isinstance(raw_body, dict) or raw_body.get("type") != "doc":
        # Occasionally older issues have plain-string bodies.
        raw_body = {
            "version": 1,
            "type": "doc",
            "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": str(raw_body) if raw_body else ""}
            ]}],
        }

    body_with_header = _prepend_to_doc(raw_body, _italic_paragraph(header_text))

    return {
        "body": body_with_header,
        "source_author": author_name,
        "source_created": created,
    }


# ---------------------------------------------------------------------------
# Issue transform
# ---------------------------------------------------------------------------

def transform_issue_rest(
    issue: Dict[str, Any],
    comments: List[Dict[str, Any]],
    mapping: Dict[str, str],
    cfg: MigrationConfig,
) -> Dict[str, Any]:
    """Transform a single raw issue + its comments into a Strategy B payload."""
    fields = issue.get("fields") or {}

    source_key: str = issue.get("key") or ""
    summary: str = fields.get("summary") or ""
    source_status: str = (fields.get("status") or {}).get("name") or ""

    issuetype_obj = fields.get("issuetype") or {}
    issuetype_name: str = issuetype_obj.get("name") or "Task"
    is_subtask: bool = bool(issuetype_obj.get("subtask", False))

    priority_obj = fields.get("priority")
    priority = {"name": priority_obj["name"]} if isinstance(priority_obj, dict) and priority_obj.get("name") else None

    labels: List[str] = fields.get("labels") or []
    components: List[Dict[str, str]] = [
        {"name": c["name"]} for c in (fields.get("components") or [])
        if isinstance(c, dict) and c.get("name")
    ]

    parent_obj = fields.get("parent")
    source_parent_key: Optional[str] = parent_obj.get("key") if isinstance(parent_obj, dict) else None

    # -- User mapping --------------------------------------------------------
    reporter_obj = fields.get("reporter")
    assignee_obj = fields.get("assignee")

    reporter_email, reporter_legacy = resolve_user(
        _user_email(reporter_obj), mapping, cfg.unmapped_user_placeholder
    )
    assignee_email, assignee_legacy = resolve_user(
        _user_email(assignee_obj), mapping, cfg.unmapped_user_placeholder
    )

    if not reporter_legacy and reporter_obj and not _user_email(reporter_obj):
        reporter_legacy = _user_legacy_id(reporter_obj)
    if not assignee_legacy and assignee_obj and not _user_email(assignee_obj):
        assignee_legacy = _user_legacy_id(assignee_obj)

    # -- Description (ADF passthrough) ---------------------------------------
    description: Any = fields.get("description")
    if not isinstance(description, dict):
        description = None

    # Prepend source issue key so it's always visible in Workspace B.
    description = _prepend_to_doc(description, _italic_paragraph(f"Migrated from: {source_key}"))

    # For Strategy B (REST) there are no "extra columns" — the only way to
    # preserve unmapped-user identity is in the description.  Always append
    # the legacy block when there are unmapped users, regardless of
    # legacy_info_strategy.
    has_legacy = bool(reporter_legacy or assignee_legacy)
    if has_legacy:
        description = _append_legacy_block_adf(description, reporter_legacy, assignee_legacy)

    # -- Date fields ---------------------------------------------------------
    due_date: Any = fields.get("duedate") or None
    start_date: Any = fields.get(cfg.start_date_field) or None

    # -- Fields payload ------------------------------------------------------
    fields_payload: Dict[str, Any] = {
        "summary": summary,
        "description": description,
        "issuetype": {"name": issuetype_name},
        "labels": labels,
        "components": components,
    }
    if due_date:
        fields_payload["duedate"] = due_date
    if start_date:
        fields_payload[cfg.start_date_field] = start_date
    if priority:
        fields_payload["priority"] = priority
    if reporter_email:
        fields_payload["reporter"] = {"email": reporter_email}
    if assignee_email:
        fields_payload["assignee"] = {"email": assignee_email}
    # parent key is filled in by write_rest.py once the dest key is known
    if source_parent_key:
        fields_payload["_source_parent_key"] = source_parent_key

    return {
        "source_key": source_key,
        "source_parent_key": source_parent_key,
        "source_status": source_status,
        "is_subtask": is_subtask,
        "fields": fields_payload,
        "legacy_reporter": reporter_legacy,
        "legacy_assignee": assignee_legacy,
        "comments": [_transform_comment(c) for c in comments],
    }


def transform_issues_rest(
    raw_issues: List[Dict[str, Any]],
    comments_by_key: Dict[str, List[Dict[str, Any]]],
    mapping: Dict[str, str],
    cfg: MigrationConfig,
) -> List[Dict[str, Any]]:
    """
    Transform all raw issues into Strategy B payloads.

    Returns two groups in order: non-subtasks first, then subtasks.
    This ensures parents always exist before their children are created.
    """
    transformed: List[Dict[str, Any]] = []
    errors = 0

    for issue in raw_issues:
        key = issue.get("key", "<unknown>")
        try:
            item = transform_issue_rest(
                issue,
                comments_by_key.get(key, []),
                mapping,
                cfg,
            )
            transformed.append(item)
        except Exception as exc:
            print(f"[transform_rest] Warning: skipping '{key}': {exc}")
            errors += 1

    if errors:
        print(f"[transform_rest] {errors} issue(s) skipped.")

    # Sort: parents before subtasks so write_rest.py can resolve parent keys.
    parents = [i for i in transformed if not i["is_subtask"]]
    subtasks = [i for i in transformed if i["is_subtask"]]
    ordered = parents + subtasks

    print(f"[transform_rest] {len(parents)} issues, {len(subtasks)} subtasks ready.")
    return ordered
