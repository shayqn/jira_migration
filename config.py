"""
config.py – Load configuration for the Jira migration.

Priority order (highest to lowest):
  1. Environment variables
  2. config.yaml (if present)

Usage:
  from config import load_config
  cfg = load_config()
  print(cfg.jira_a.base_url)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

# Optional YAML support — only required if you use config.yaml
try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

CONFIG_YAML_PATH = Path(__file__).parent / "config.yaml"


@dataclass
class SiteConfig:
    base_url: str          # e.g. https://mycompany.atlassian.net
    email: str             # service-account email
    api_token: str         # Atlassian API token


@dataclass
class MigrationConfig:
    jira_a: SiteConfig
    jira_b: Optional[SiteConfig]       # None until Strategy B is needed

    # How to preserve identities of users who are NOT in the user mapping.
    # "extra_columns"      → populate OriginalReporterLegacy / OriginalAssigneeLegacy columns
    # "append_description" → append a block to the Description field
    # "both"               → do both
    legacy_info_strategy: str = "extra_columns"

    # When a user has no mapping, what to put in the *target* email column.
    # Set to "" to leave blank, or e.g. "noreply@newco.com" for a catchall.
    unmapped_user_placeholder: str = ""

    # Directory (relative to CWD, or absolute) where CSV files are written.
    output_dir: str = "output"

    # Maximum issues to fetch per API page (Jira max is 100).
    page_size: int = 100

    # Strategy B: explicit issue-type name mapping from Workspace A → Workspace B.
    # e.g. {"Story": "Task", "Epic": "Task"}
    # Types not listed here are passed through unchanged.
    issue_type_map: Dict[str, str] = field(default_factory=dict)

    # Strategy B: fallback issue type when a type from Workspace A doesn't exist
    # in Workspace B and has no entry in issue_type_map.
    fallback_issue_type: str = "Task"

    # The Jira custom field ID used for "Start date" in Workspace A.
    # Most Jira Cloud projects use customfield_10015; change if yours differs.
    start_date_field: str = "customfield_10015"


def _require(value: Optional[str], name: str) -> str:
    if not value:
        raise EnvironmentError(
            f"Required config value '{name}' is missing. "
            "Set it as an environment variable or in config.yaml."
        )
    return value


def load_config() -> MigrationConfig:
    """
    Load config from environment variables, falling back to config.yaml.
    Environment variables always take precedence.
    """
    yaml_data: dict = {}
    if CONFIG_YAML_PATH.exists():
        if not _YAML_AVAILABLE:
            raise ImportError(
                "config.yaml found but PyYAML is not installed. "
                "Run: pip install pyyaml  — or use environment variables instead."
            )
        with CONFIG_YAML_PATH.open() as f:
            yaml_data = yaml.safe_load(f) or {}

    def _get(env_var: str, *yaml_path: str, default: Optional[str] = None) -> Optional[str]:
        """Return env var if set, else walk yaml_path in yaml_data, else default."""
        val = os.environ.get(env_var)
        if val:
            return val
        node = yaml_data
        for key in yaml_path:
            if not isinstance(node, dict):
                return default
            node = node.get(key)
        return node if node is not None else default

    jira_a = SiteConfig(
        base_url=_require(_get("JIRA_A_BASE_URL", "jira_a", "base_url"), "JIRA_A_BASE_URL").rstrip("/"),
        email=_require(_get("JIRA_A_EMAIL", "jira_a", "email"), "JIRA_A_EMAIL"),
        api_token=_require(_get("JIRA_A_API_TOKEN", "jira_a", "api_token"), "JIRA_A_API_TOKEN"),
    )

    # Workspace B is optional until Strategy B is implemented.
    b_url = _get("JIRA_B_BASE_URL", "jira_b", "base_url")
    b_email = _get("JIRA_B_EMAIL", "jira_b", "email")
    b_token = _get("JIRA_B_API_TOKEN", "jira_b", "api_token")
    jira_b = (
        SiteConfig(
            base_url=b_url.rstrip("/"),
            email=b_email,
            api_token=b_token,
        )
        if b_url and b_email and b_token
        else None
    )

    legacy_strategy = _get(
        "LEGACY_INFO_STRATEGY", "migration", "legacy_info_strategy",
        default="extra_columns",
    )
    if legacy_strategy not in ("extra_columns", "append_description", "both"):
        raise ValueError(
            f"Invalid legacy_info_strategy '{legacy_strategy}'. "
            "Must be one of: extra_columns, append_description, both."
        )

    # issue_type_map is only loadable from config.yaml (not practical as env var).
    raw_type_map = yaml_data.get("migration", {}).get("issue_type_map") or {}
    issue_type_map = {str(k): str(v) for k, v in raw_type_map.items()} if isinstance(raw_type_map, dict) else {}

    return MigrationConfig(
        jira_a=jira_a,
        jira_b=jira_b,
        legacy_info_strategy=legacy_strategy,
        unmapped_user_placeholder=_get(
            "UNMAPPED_USER_PLACEHOLDER", "migration", "unmapped_user_placeholder",
            default="",
        ),
        output_dir=_get("OUTPUT_DIR", "migration", "output_dir", default="output"),
        page_size=int(_get("PAGE_SIZE", "migration", "page_size", default="100")),
        issue_type_map=issue_type_map,
        fallback_issue_type=_get(
            "FALLBACK_ISSUE_TYPE", "migration", "fallback_issue_type", default="Task"
        ),
        start_date_field=_get(
            "START_DATE_FIELD", "migration", "start_date_field", default="customfield_10015"
        ),
    )
