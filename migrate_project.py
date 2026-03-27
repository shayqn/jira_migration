#!/usr/bin/env python3
"""
migrate_project.py – CLI entry point for the Jira A→B migration.

Strategy A (CSV):
  Extract issues from Workspace A, transform them, and write a CSV file
  for manual import into Workspace B via Jira's CSV importer.

Strategy B (REST):
  Extract issues + comments from Workspace A, transform them (preserving
  full ADF rich-text), and create issues + comments directly in Workspace B
  via the Jira Cloud REST API.

Usage examples
--------------
  # Set credentials:
  export JIRA_A_BASE_URL=https://source.atlassian.net
  export JIRA_A_EMAIL=you@source.com
  export JIRA_A_API_TOKEN=<token_a>

  # Strategy A – produce a CSV:
  python migrate_project.py --project PROJ1

  # Strategy B – push directly to Workspace B:
  export JIRA_B_BASE_URL=https://dest.atlassian.net
  export JIRA_B_EMAIL=you@dest.com
  export JIRA_B_API_TOKEN=<token_b>
  python migrate_project.py --project PROJ1 --strategy rest

  # Strategy B to a differently-named project in Workspace B:
  python migrate_project.py --project PROJ1 --strategy rest --project-b NEWPROJ
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import load_config, MigrationConfig
from user_mapping import load_user_mapping
from extract import fetch_all_issues, fetch_comments_for_issues, supplement_user_emails
from transform import transform_issues
from write_csv import write_issues_csv
from transform_rest import transform_issues_rest
from write_rest import write_issues_rest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate a Jira project from Workspace A to Workspace B.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--project", "-p",
        required=True,
        metavar="PROJKEY",
        help="Project key in Workspace A, e.g. PROJ1",
    )
    parser.add_argument(
        "--strategy",
        choices=["csv", "rest"],
        default="csv",
        help="Migration strategy: 'csv' (default) writes a CSV; 'rest' pushes directly to Workspace B",
    )
    parser.add_argument(
        "--project-b",
        default=None,
        metavar="PROJKEY",
        help="Project key in Workspace B (Strategy B only). Defaults to --project if not set.",
    )
    parser.add_argument(
        "--mapping", "-m",
        default=str(Path(__file__).parent / "user_mapping.csv"),
        metavar="FILE",
        help="Path to user-mapping CSV (default: user_mapping.csv next to this script)",
    )
    parser.add_argument(
        "--legacy-strategy",
        choices=["extra_columns", "append_description", "both"],
        default=None,
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
        help="Directory for output CSV files — Strategy A only (default: output/)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg: MigrationConfig = load_config()

    if args.legacy_strategy:
        cfg.legacy_info_strategy = args.legacy_strategy
    if args.output_dir:
        cfg.output_dir = args.output_dir

    project_key: str = args.project.upper()
    strategy: str = args.strategy

    print(f"\n{'='*60}")
    print(f"  Migrating project: {project_key}")
    print(f"  Strategy:          {strategy.upper()}")
    print(f"  Source:            {cfg.jira_a.base_url}")
    if strategy == "rest":
        dest_project_key = (args.project_b or project_key).upper()
        if cfg.jira_b is None:
            print(
                "\n[main] ERROR: Strategy B requires Workspace B credentials.\n"
                "Set JIRA_B_BASE_URL, JIRA_B_EMAIL, and JIRA_B_API_TOKEN.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"  Destination:       {cfg.jira_b.base_url}  (project: {dest_project_key})")
    print(f"  Legacy strategy:   {cfg.legacy_info_strategy}")
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
        print(f"[main] No issues found for project '{project_key}'. Nothing to migrate.")
        sys.exit(0)

    # --- Supplement missing user emails -------------------------------------
    # Jira Cloud often omits emailAddress from user objects in search results.
    # Look up each missing email via the single-user endpoint so the user
    # mapping can match on email address.
    supplement_user_emails(cfg.jira_a, raw_issues)

    # --- Strategy A: CSV ----------------------------------------------------
    if strategy == "csv":
        rows = transform_issues(raw_issues, mapping, cfg)

        output_dir = Path(cfg.output_dir)
        if not output_dir.is_absolute():
            output_dir = Path(__file__).parent / output_dir
        output_path = output_dir / f"{project_key}_issues.csv"
        write_issues_csv(rows, output_path)

        print(f"\n[main] Done.  CSV written to: {output_path}")
        print(f"[main] Import via: Jira → Project settings → Import issues → CSV")

    # --- Strategy B: REST ---------------------------------------------------
    elif strategy == "rest":
        comments_by_key = fetch_comments_for_issues(cfg.jira_a, raw_issues)
        transformed = transform_issues_rest(raw_issues, comments_by_key, mapping, cfg)

        issues_created, comments_posted, errors = write_issues_rest(
            transformed, cfg.jira_b, dest_project_key, cfg
        )

        if errors:
            print(f"\n[main] Completed with {errors} error(s) — review output above.")
            sys.exit(1)
        else:
            print(f"\n[main] Migration complete: {issues_created} issues, {comments_posted} comments.")


if __name__ == "__main__":
    main()
