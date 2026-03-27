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


# ADF node types that reference workspace-specific attachment IDs.
# Posting these to a different workspace always fails with ATTACHMENT_VALIDATION_ERROR.
_MEDIA_BLOCK_TYPES = {"mediaSingle", "mediaGroup"}
_MEDIA_INLINE_TYPES = {"mediaInline", "media"}


def _sanitize_adf(node: Any) -> Any:
    """
    Recursively walk an ADF node and replace media/attachment nodes with
    text placeholders.

    Jira Cloud attachment IDs are workspace-specific — any ADF that embeds
    them will be rejected (400 ATTACHMENT_VALIDATION_ERROR) when posted to a
    different workspace.  We replace them with a visible note rather than
    silently dropping them.
    """
    if not isinstance(node, dict):
        return node

    node_type = node.get("type", "")

    # Block-level media (images, file previews) → italic placeholder paragraph
    if node_type in _MEDIA_BLOCK_TYPES:
        return _italic_paragraph("[Attachment not migrated]")

    # Inline media reference → plain text node
    if node_type in _MEDIA_INLINE_TYPES:
        return {"type": "text", "text": "[attachment]"}

    # Recurse into content array, filtering out any None results
    content = node.get("content")
    if content:
        sanitized = [_sanitize_adf(child) for child in content if child is not None]
        return {**node, "content": sanitized}

    return node


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


def _user_account_id(obj: Any) -> Optional[str]:
    if not isinstance(obj, dict):
        return None
    return obj.get("accountId") or None


def _user_legacy_id(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    return obj.get("emailAddress") or obj.get("displayName") or obj.get("accountId") or ""


def _resolve_user(
    user_obj: Any,
    mapping: Dict[str, str],
    placeholder: str,
) -> tuple:
    """
    Resolve a Jira user object to (target_email, legacy_identity).

    Tries source email first, then accountId as a fallback — Jira Cloud often
    omits emailAddress from API responses due to privacy settings.
    Legacy identity is always the human-readable display name, never an accountId.
    """
    email      = _user_email(user_obj)
    account_id = _user_account_id(user_obj)

    # Email lookup
    if email and email in mapping:
        return mapping[email], ""

    # accountId fallback
    if account_id and account_id in mapping:
        return mapping[account_id], ""

    # Not mapped — return placeholder and best human-readable identity
    legacy = _user_legacy_id(user_obj)
    return placeholder, legacy


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

    raw_body = _sanitize_adf(raw_body)
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

    reporter_email, reporter_legacy = _resolve_user(reporter_obj, mapping, cfg.unmapped_user_placeholder)
    assignee_email, assignee_legacy = _resolve_user(assignee_obj, mapping, cfg.unmapped_user_placeholder)

    # -- Description (ADF passthrough) ---------------------------------------
    description: Any = fields.get("description")
    if not isinstance(description, dict):
        description = None
    if description is not None:
        description = _sanitize_adf(description)

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


def _topological_sort(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort issues so every parent is ordered before its children, at any depth.

    Uses Kahn's algorithm (BFS).  Issues whose parent is not in the migrated
    set (e.g. the parent belongs to a different project) are treated as roots.
    Relative order among siblings is preserved from the input list.

    If a cycle is detected (shouldn't occur in valid Jira data) the remaining
    issues are appended at the end with a warning rather than crashing.
    """
    by_key: Dict[str, Dict[str, Any]] = {item["source_key"]: item for item in issues}

    # Build children map and in-degree count based on parent_key relationships
    # that are within this migration batch.
    children: Dict[str, List[str]] = {key: [] for key in by_key}
    in_degree: Dict[str, int] = {key: 0 for key in by_key}

    for item in issues:
        parent_key = item.get("source_parent_key")
        if parent_key and parent_key in by_key:
            children[parent_key].append(item["source_key"])
            in_degree[item["source_key"]] += 1

    # Seed the queue with roots (issues with no parent in this batch),
    # preserving the original relative order.
    queue: List[str] = [
        item["source_key"] for item in issues
        if in_degree[item["source_key"]] == 0
    ]

    ordered: List[Dict[str, Any]] = []
    while queue:
        key = queue.pop(0)
        ordered.append(by_key[key])
        for child_key in children[key]:
            in_degree[child_key] -= 1
            if in_degree[child_key] == 0:
                queue.append(child_key)

    # Cycle guard — append any remaining issues that never reached in_degree 0.
    remaining = [by_key[k] for k in by_key if in_degree[k] > 0]
    if remaining:
        cycle_keys = [i["source_key"] for i in remaining]
        print(
            f"[transform_rest] Warning: {len(remaining)} issue(s) appear to be in a "
            f"parent cycle and will be created without a parent link: {cycle_keys}"
        )
        ordered.extend(remaining)

    return ordered


def transform_issues_rest(
    raw_issues: List[Dict[str, Any]],
    comments_by_key: Dict[str, List[Dict[str, Any]]],
    mapping: Dict[str, str],
    cfg: MigrationConfig,
) -> List[Dict[str, Any]]:
    """
    Transform all raw issues into Strategy B payloads, topologically sorted
    so every parent is created before its children regardless of issue type
    or original creation order.
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

    ordered = _topological_sort(transformed)

    children_count = sum(1 for i in ordered if i.get("source_parent_key"))
    roots_count = len(ordered) - children_count
    print(f"[transform_rest] {len(ordered)} issues ready ({roots_count} roots, {children_count} children).")
    return ordered
