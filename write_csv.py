"""
write_csv.py – Write transformed issue rows to a CSV file.

This module is intentionally thin: it just takes a list of row dicts and a
path and writes them out.  Strategy B would replace (or sit alongside) this
module with a Workspace-B REST writer that consumes the same row dicts.

Usage:
  from write_csv import write_issues_csv
  write_issues_csv(rows, output_path="output/PROJ1_issues.csv")
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import List, Dict

from transform import CSV_COLUMNS


def write_issues_csv(
    rows: List[Dict[str, str]],
    output_path: str | Path,
) -> None:
    """
    Write *rows* to a UTF-8 CSV at *output_path*.

    - Creates parent directories automatically.
    - Column order follows CSV_COLUMNS defined in transform.py.
    - Uses csv.QUOTE_ALL so that multi-line Description cells survive import.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        print(f"[write_csv] No rows to write – skipping '{path}'.")
        return

    try:
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=CSV_COLUMNS,
                quoting=csv.QUOTE_ALL,
                extrasaction="ignore",  # silently drop any extra keys in row dicts
            )
            writer.writeheader()
            writer.writerows(rows)
    except OSError as exc:
        print(f"[write_csv] Failed to write '{path}': {exc}", file=sys.stderr)
        raise

    print(f"[write_csv] Wrote {len(rows)} rows → '{path}'.")
