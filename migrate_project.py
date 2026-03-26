#!/usr/bin/env python3
"""
migrate_project.py – CLI entry point for the Jira A→B migration.

Strategy A (CSV): extract issues from Workspace A, transform them, and write
a CSV file that you import manually into Workspace B via Jira's CSV importer.

Usage:
  # Set credentials via environment variables:
  export JIRA_A_BASE_URL=https://your-source.atlassian.net
  export JIRA_A_EMAIL=you@source.com
  export JIRA_A_API_TOKEN=<your_api_token>

  # Run for a single project:
  python migrate_project.py --project PROJ1

  # Run with a custom user-mapping file:
  python migrate_project.py --project PROJ1 --mapping user_mapping.csv

  # Override legacy-info strategy at the command line:
  python migrate_project.py --project PROJ1 --legacy-strategy append_description

  # The output CSV will be written to:
  #   output/PROJ1_issues.csv   (or whatever OUTPUT_DIR is set to)

Adding Strategy B later
-----------------------
When you're ready to push directly to Workspace B via REST:
  1. Create a write_rest.py module with a write_issues_rest(rows, site_config) function.
  2. In main() below, swap out write_issues_csv() for write_issues_rest().
  3. The extraction + transformation code is unchanged.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure the migration/ package directory is on sys.path when run directly.
sys.path.insert(0, str(Path(__file__).parent))

from config import load_config, MigrationConfig
from user_mapping import load_user_mapping
from extract import fetch_all_issues
from transform import transform_issues
from write_csv import write_issues_csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a Jira project from Workspace A to a CSV for import into Workspace B.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--project", "-p",
        required=True,
        metavar="PROJKEY",
        help="Jira project key in Workspace A, e.g. PROJ1",
    )
    parser.add_argument(
        "--mapping", "-m",
        default=str(Path(__file__).parent / "user_mapping.csv"),
        metavar="FILE",
        help="Path to the user-mapping CSV file (default: user_mapping.csv next to this script)",
    )
    parser.add_argument(
        "--legacy-strategy",
        choices=["extra_columns", "append_description", "both"],
        default=None,
        metavar="STRATEGY",
        help=(
            "How to preserve unmapped-user identities. "
            "Overrides LEGACY_INFO_STRATEGY env var / config.yaml. "
            "Choices: extra_columns (default), append_description, both"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help="Directory for output CSV files (default: output/ next to this script)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # --- Config -------------------------------------------------------------
    cfg: MigrationConfig = load_config()

    if args.legacy_strategy:
        cfg.legacy_info_strategy = args.legacy_strategy

    if args.output_dir:
        cfg.output_dir = args.output_dir

    # Resolve output_dir relative to this script's directory if not absolute.
    output_dir = Path(cfg.output_dir)
    if not output_dir.is_absolute():
        output_dir = Path(__file__).parent / output_dir

    project_key: str = args.project.upper()
    print(f"\n{'='*60}")
    print(f"  Migrating project: {project_key}")
    print(f"  Source:            {cfg.jira_a.base_url}")
    print(f"  Legacy strategy:   {cfg.legacy_info_strategy}")
    print(f"  Output dir:        {output_dir}")
    print(f"{'='*60}\n")

    # --- User mapping -------------------------------------------------------
    mapping = load_user_mapping(args.mapping)

    # --- Extract ------------------------------------------------------------
    raw_issues = fetch_all_issues(
        site=cfg.jira_a,
        project_key=project_key,
        page_size=cfg.page_size,
    )

    if not raw_issues:
        print(f"[main] No issues found for project '{project_key}'. Nothing to export.")
        sys.exit(0)

    # --- Transform ----------------------------------------------------------
    rows = transform_issues(raw_issues, mapping, cfg)

    # --- Write (Strategy A – CSV) ------------------------------------------
    output_path = output_dir / f"{project_key}_issues.csv"
    write_issues_csv(rows, output_path)

    print(f"\n[main] Done.  CSV written to: {output_path}")
    print(f"[main] Import it into Workspace B via:")
    print(f"       Jira → Project settings → Import issues → CSV")


if __name__ == "__main__":
    main()
