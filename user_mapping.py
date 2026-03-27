"""
user_mapping.py – Load the user mapping CSV and expose a lookup dict.

CSV format (with header row):
  source_email,source_account_id,target_email
  alice@oldco.com,712020:abc123,alice@newco.com
  bob@oldco.com,,bob@newco.com        ← source_account_id is optional

Both source_email and source_account_id (Jira accountId) are indexed in the
returned dict so callers can look up by whichever identifier is available.
Jira Cloud often hides emailAddress in API responses, so accountId is the
reliable fallback.

Usage:
  from user_mapping import load_user_mapping
  mapping = load_user_mapping("user_mapping.csv")
  target = mapping.get("alice@oldco.com")        # look up by email
  target = mapping.get("712020:abc123")           # look up by accountId
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, Optional


def load_user_mapping(path: str | Path) -> Dict[str, str]:
    """
    Load a user-mapping CSV and return a dict keyed by both source_email and
    source_account_id so callers can look up by whichever is available.

    - The file must have 'source_email' and 'target_email' columns (case-insensitive).
    - 'source_account_id' is optional; rows without it are still loaded by email.
    - Blank lines and rows with no source identifier are skipped.
    - Duplicate keys: last row wins (with a warning printed).
    - If the file does not exist, returns an empty dict and prints a warning.
    """
    mapping: Dict[str, str] = {}
    file_path = Path(path)

    if not file_path.exists():
        print(
            f"[user_mapping] Warning: mapping file '{file_path}' not found. "
            "All users will be treated as unmapped."
        )
        return mapping

    with file_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            print(f"[user_mapping] Warning: '{file_path}' appears to be empty.")
            return mapping

        lower_fields = [h.strip().lower() for h in reader.fieldnames]
        if "source_email" not in lower_fields or "target_email" not in lower_fields:
            raise ValueError(
                f"'{file_path}' must have 'source_email' and 'target_email' columns. "
                f"Found: {reader.fieldnames}"
            )

        for row in reader:
            row_lower = {k.strip().lower(): v.strip() for k, v in row.items()}
            src_email      = row_lower.get("source_email", "")
            src_account_id = row_lower.get("source_account_id", "")
            tgt            = row_lower.get("target_email", "")

            if not src_email and not src_account_id:
                continue  # skip empty rows

            if src_email:
                if src_email in mapping:
                    print(f"[user_mapping] Warning: duplicate source_email '{src_email}' – last entry wins.")
                mapping[src_email] = tgt

            if src_account_id:
                if src_account_id in mapping:
                    print(f"[user_mapping] Warning: duplicate source_account_id '{src_account_id}' – last entry wins.")
                mapping[src_account_id] = tgt

    emails = sum(1 for k in mapping if "@" in k)
    ids    = len(mapping) - emails
    print(f"[user_mapping] Loaded {emails} email + {ids} accountId mapping(s) from '{file_path}'.")
    return mapping


def resolve_user(
    source_email: Optional[str],
    mapping: Dict[str, str],
    unmapped_placeholder: str = "",
) -> tuple[str, str]:
    """
    Resolve a source-site email to a (target_email, legacy_email) tuple.

    Returns:
      target_email   – mapped email, or unmapped_placeholder if not in mapping
      legacy_email   – source_email if unmapped (preserve identity), else ""
    """
    if not source_email:
        return unmapped_placeholder, ""

    if source_email in mapping:
        return mapping[source_email], ""  # mapped → no legacy entry needed

    return unmapped_placeholder, source_email  # unmapped → preserve as legacy
