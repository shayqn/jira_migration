"""
transform.py – Convert raw Jira issue dicts into normalized row dicts.

The output row dict is intentionally strategy-agnostic: it's a flat dict of
strings that write_csv.py can dump to CSV, or a future Strategy-B writer can
map to Jira REST API fields.

Usage:
  from transform import transform_issues
  rows = transform_issues(raw_issues, mapping, cfg)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from adf_utils import adf_to_text
from config import MigrationConfig
from user_mapping import resolve_user

# Ordered list of CSV column names.  write_csv.py uses this as the header.
CSV_COLUMNS = [
    "IssueKey",
    "Summary",
    "Description",
    "Status",
    "IssueType",
    "Priority",
    "ReporterEmail",
    "AssigneeEmail",
    "OriginalReporterLegacy",
    "OriginalAssigneeLegacy",
    "Created",
    "Updated",
    "Resolution",
    "ResolutionDate",
    "Labels",
    "Components",
    "ParentKey",
    "DueDate",
    "StartDate",
]


def _get_field(issue: Dict[str, Any], *path: str) -> Any:
    """Safely walk a nested dict path; returns None if any key is missing."""
    node: Any = issue
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _extract_description(issue: Dict[str, Any]) -> str:
    """Convert the ADF description field to plain text (may be None)."""
    raw = _get_field(issue, "fields", "description")
    return adf_to_text(raw).strip()


def _build_legacy_block(
    reporter_legacy: str,
    assignee_legacy: str,
) -> str:
    """Return a formatted text block for appending to Description."""
    lines = ["", "----", "Legacy user info (from source Jira):"]
    if reporter_legacy:
        lines.append(f"Original reporter: {reporter_legacy}")
    if assignee_legacy:
        lines.append(f"Original assignee: {assignee_legacy}")
    return "\n".join(lines)


def transform_issue(
    issue: Dict[str, Any],
    mapping: Dict[str, str],
    cfg: MigrationConfig,
) -> Dict[str, str]:
    """
    Transform a single raw Jira issue dict into a flat CSV row dict.

    Extend this function (or add a post-processing hook) to support
    Strategy B: instead of returning a CSV-ready dict, return whatever
    structure the Workspace-B REST writer needs.
    """
    fields = issue.get("fields") or {}

    # -- Core fields ---------------------------------------------------------
    issue_key: str = issue.get("key") or ""
    summary: str = fields.get("summary") or ""

    issuetype: str = _get_field(issue, "fields", "issuetype", "name") or ""
    status: str = _get_field(issue, "fields", "status", "name") or ""
    priority: str = _get_field(issue, "fields", "priority", "name") or ""

    created: str = fields.get("created") or ""
    updated: str = fields.get("updated") or ""

    resolution_obj = fields.get("resolution")
    resolution: str = resolution_obj.get("name") if isinstance(resolution_obj, dict) else ""
    resolution_date: str = fields.get("resolutiondate") or ""

    # -- Labels & Components -------------------------------------------------
    labels_raw: list = fields.get("labels") or []
    labels: str = ", ".join(str(l) for l in labels_raw)

    components_raw: list = fields.get("components") or []
    components: str = ", ".join(c.get("name", "") for c in components_raw if isinstance(c, dict))

    # -- Parent key (sub-tasks) ----------------------------------------------
    parent_obj = fields.get("parent")
    parent_key: str = parent_obj.get("key", "") if isinstance(parent_obj, dict) else ""

    due_date: str = fields.get("duedate") or ""
    start_date: str = fields.get(cfg.start_date_field) or ""

    # -- User mapping --------------------------------------------------------
    # Jira Cloud often omits emailAddress due to visibility settings.
    # We use email for the mapping lookup (since that's what user_mapping.csv
    # contains), and fall back to "displayName (accountId)" as the legacy
    # identifier when email isn't available.

    def _user_email(obj: Any) -> Optional[str]:
        """Return emailAddress if present, else None."""
        if not isinstance(obj, dict):
            return None
        return obj.get("emailAddress") or None

    def _user_legacy_id(obj: Any) -> str:
        """Return the best human-readable identifier for an unmapped user."""
        if not isinstance(obj, dict):
            return ""
        email = obj.get("emailAddress") or ""
        display = obj.get("displayName") or ""
        account = obj.get("accountId") or ""
        if email:
            return email
        return display or account

    reporter_obj = fields.get("reporter")
    assignee_obj = fields.get("assignee")

    reporter_email, reporter_legacy = resolve_user(
        _user_email(reporter_obj), mapping, cfg.unmapped_user_placeholder
    )
    assignee_email, assignee_legacy = resolve_user(
        _user_email(assignee_obj), mapping, cfg.unmapped_user_placeholder
    )

    # If a user was unmapped and we have no email, fall back to displayName/accountId
    # so the legacy columns are never silently empty for real users.
    if not reporter_legacy and reporter_obj and not _user_email(reporter_obj):
        reporter_legacy = _user_legacy_id(reporter_obj)
    if not assignee_legacy and assignee_obj and not _user_email(assignee_obj):
        assignee_legacy = _user_legacy_id(assignee_obj)

    # -- Description ---------------------------------------------------------
    description: str = _extract_description(issue)

    strategy = cfg.legacy_info_strategy
    has_legacy = bool(reporter_legacy or assignee_legacy)

    if has_legacy and strategy in ("append_description", "both"):
        description += _build_legacy_block(reporter_legacy, assignee_legacy)

    # Only populate legacy columns when strategy calls for it.
    if strategy in ("extra_columns", "both"):
        original_reporter_legacy = reporter_legacy
        original_assignee_legacy = assignee_legacy
    else:
        original_reporter_legacy = ""
        original_assignee_legacy = ""

    return {
        "IssueKey": issue_key,
        "Summary": summary,
        "Description": description,
        "Status": status,
        "IssueType": issuetype,
        "Priority": priority,
        "ReporterEmail": reporter_email,
        "AssigneeEmail": assignee_email,
        "OriginalReporterLegacy": original_reporter_legacy,
        "OriginalAssigneeLegacy": original_assignee_legacy,
        "Created": created,
        "Updated": updated,
        "Resolution": resolution,
        "ResolutionDate": resolution_date,
        "Labels": labels,
        "Components": components,
        "ParentKey": parent_key,
        "DueDate": due_date,
        "StartDate": start_date,
    }


def transform_issues(
    raw_issues: List[Dict[str, Any]],
    mapping: Dict[str, str],
    cfg: MigrationConfig,
) -> List[Dict[str, str]]:
    """
    Transform a list of raw Jira issue dicts into a list of row dicts.

    Logs a warning and skips any issue that fails transformation rather
    than aborting the entire project run.
    """
    rows: List[Dict[str, str]] = []
    errors = 0

    for issue in raw_issues:
        try:
            row = transform_issue(issue, mapping, cfg)
            rows.append(row)
        except Exception as exc:
            key = issue.get("key", "<unknown>")
            print(f"[transform] Warning: skipping issue '{key}' due to error: {exc}")
            errors += 1

    if errors:
        print(f"[transform] {errors} issue(s) skipped due to transform errors.")

    print(f"[transform] Transformed {len(rows)} issues.")
    return rows
