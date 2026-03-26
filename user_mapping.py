"""
user_mapping.py – Load the user mapping CSV and expose a lookup dict.

CSV format (with header row):
  source_email,target_email
  alice@oldco.com,alice@newco.com
  bob@oldco.com,bob@newco.com

Usage:
  from user_mapping import load_user_mapping
  mapping = load_user_mapping("user_mapping.csv")
  target = mapping.get("alice@oldco.com")  # → "alice@newco.com"
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, Optional


def load_user_mapping(path: str | Path) -> Dict[str, str]:
    """
    Load a user-mapping CSV and return {source_email: target_email}.

    - The file must have a header row with at least the columns
      'source_email' and 'target_email' (case-insensitive).
    - Blank lines and lines where both columns are empty are skipped.
    - Duplicate source emails: last row wins (with a warning printed).
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

        # Normalize header names to lowercase for robustness.
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
            # Re-key using lowercase field names for safety.
            row_lower = {k.strip().lower(): v.strip() for k, v in row.items()}
            src = row_lower.get("source_email", "")
            tgt = row_lower.get("target_email", "")

            if not src:
                continue  # skip rows with no source

            if src in mapping:
                print(
                    f"[user_mapping] Warning: duplicate source_email '{src}' – "
                    "last entry wins."
                )
            mapping[src] = tgt

    print(f"[user_mapping] Loaded {len(mapping)} user mapping(s) from '{file_path}'.")
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
