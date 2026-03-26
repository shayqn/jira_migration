# jira_migration

A Python tool for migrating Jira projects between two Atlassian Cloud workspaces using the Jira REST API. Does not use JCMA or Atlassian's copy-plan tool.

---

## How it works

Two strategies are supported:

| Strategy | How | Best for |
|---|---|---|
| **A – CSV** | Exports issues to a CSV file for manual import via Jira's built-in CSV importer | Quick migrations, no dest credentials needed |
| **B – REST** | Creates issues, comments, and transitions directly in Workspace B via the REST API | Full fidelity: ADF rich text, comments, statuses, dates |

Both strategies extract from Workspace A, apply a user mapping, and preserve the identity of unmapped users in the issue description.

---

## What gets migrated

| Field | Strategy A (CSV) | Strategy B (REST) |
|---|---|---|
| Summary | ✅ | ✅ |
| Description | ✅ plain text | ✅ full ADF rich text |
| Issue type | ✅ | ✅ with type mapping + fallback |
| Status | ✅ (as text) | ✅ via workflow transitions |
| Priority | ✅ | ✅ |
| Reporter / Assignee | ✅ mapped emails | ✅ resolved to accountIds |
| Due date | ✅ | ✅ |
| Start date | ✅ | ✅ |
| Labels | ✅ | ✅ |
| Components | ✅ | ✅ |
| Parent key (subtasks) | ✅ | ✅ parents created first |
| Comments | ❌ | ✅ with original author + date header |
| Source issue key | ✅ `IssueKey` column | ✅ prepended to description |
| Unmapped user identity | ✅ extra columns or appended to description | ✅ appended to description |

---

## Prerequisites

- Python 3.9+
- A Jira Cloud API token for Workspace A (and Workspace B for Strategy B)
  - Generate at: https://id.atlassian.com/manage-profile/security/api-tokens

---

## Installation

```bash
git clone <repo-url> jira_migration
cd jira_migration
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## Configuration

Credentials and options can be set via environment variables or `config.yaml` (environment variables take precedence).

### Option A: environment variables

```bash
# Workspace A (required for both strategies)
export JIRA_A_BASE_URL=https://your-source.atlassian.net
export JIRA_A_EMAIL=you@source.com
export JIRA_A_API_TOKEN=<token>

# Workspace B (required for Strategy B only)
export JIRA_B_BASE_URL=https://your-destination.atlassian.net
export JIRA_B_EMAIL=you@destination.com
export JIRA_B_API_TOKEN=<token>
```

### Option B: config.yaml

Copy the example file and fill in your values:

```bash
cp config.yaml.example config.yaml
```

`config.yaml` is gitignored and should never be committed.

### Full config reference

| Key | Env var | Default | Description |
|---|---|---|---|
| `jira_a.base_url` | `JIRA_A_BASE_URL` | — | Source workspace URL |
| `jira_a.email` | `JIRA_A_EMAIL` | — | Source API token email |
| `jira_a.api_token` | `JIRA_A_API_TOKEN` | — | Source API token |
| `jira_b.base_url` | `JIRA_B_BASE_URL` | — | Destination workspace URL |
| `jira_b.email` | `JIRA_B_EMAIL` | — | Destination API token email |
| `jira_b.api_token` | `JIRA_B_API_TOKEN` | — | Destination API token |
| `migration.legacy_info_strategy` | `LEGACY_INFO_STRATEGY` | `extra_columns` | How to record unmapped users: `extra_columns`, `append_description`, or `both`. Strategy B always appends to description regardless. |
| `migration.unmapped_user_placeholder` | `UNMAPPED_USER_PLACEHOLDER` | `""` | Email to use for unmapped users in reporter/assignee fields. Leave blank to leave unassigned. |
| `migration.output_dir` | `OUTPUT_DIR` | `output` | Directory for Strategy A CSV files |
| `migration.page_size` | `PAGE_SIZE` | `100` | Issues per API page (Jira max: 100) |
| `migration.issue_type_map` | _(YAML only)_ | `{}` | Map issue type names from Workspace A → B, e.g. `Story: Task` |
| `migration.fallback_issue_type` | `FALLBACK_ISSUE_TYPE` | `Task` | Issue type to use when source type doesn't exist in Workspace B and has no mapping |
| `migration.start_date_field` | `START_DATE_FIELD` | `customfield_10015` | Custom field ID for "Start date" in Workspace A |

---

## User mapping

Create `user_mapping.csv` (gitignored) to map source emails to destination emails:

```csv
source_email,target_email
alice@oldco.com,alice@newco.com
bob@oldco.com,bob@newco.com
```

See `user_mapping.csv.example` for the format. Users not listed in the mapping are treated as unmapped — their original identity is preserved in the issue description.

---

## Running a migration

### Strategy A — export to CSV

```bash
python migrate_project.py --project PROJ1
```

This writes `output/PROJ1_issues.csv`. Import it in Workspace B via:
**Project settings → Import issues → CSV**

### Strategy B — push directly to Workspace B

```bash
python migrate_project.py --project PROJ1 --strategy rest
```

To migrate into a differently-named project in Workspace B:

```bash
python migrate_project.py --project PROJ1 --strategy rest --project-b NEWPROJ
```

The destination project must already exist in Workspace B before running.

### All CLI flags

```
--project, -p PROJKEY       Project key in Workspace A (required)
--strategy {csv,rest}       Migration strategy (default: csv)
--project-b PROJKEY         Project key in Workspace B (Strategy B only; defaults to --project)
--mapping, -m FILE          Path to user_mapping.csv (default: user_mapping.csv next to script)
--legacy-strategy           Override legacy_info_strategy from config
--output-dir DIR            Override output directory (Strategy A only)
```

---

## Before running Strategy B

1. **Create the destination project** in Workspace B if it doesn't exist.
2. **Configure the workflow** — the script transitions issues to their source status by name. If Workspace B's workflow uses different status names, either update the workflow to match, or issues will be left in the default status (a warning is printed).
3. **Verify issue types** — the script fetches available types from the destination project and applies `issue_type_map` + `fallback_issue_type`. Check the startup output to confirm types are resolving correctly.

---

## Limitations and known behaviors

**Comments are always posted as the API token user.** Jira Cloud does not allow posting comments as another user via the REST API, regardless of permissions. Each comment is prepended with an italic header: _"Originally posted by [name] on [date]:"_

**Source issue key is always preserved.** Each migrated issue has an italic _"Migrated from: PROJ1-42"_ line prepended to its description.

**Unmapped users.** When a user in Workspace A has no entry in `user_mapping.csv`, the reporter/assignee field is left blank (or set to `unmapped_user_placeholder`), and their original identity is appended to the issue description.

**Jira Cloud hides email addresses.** The API often omits `emailAddress` from user objects due to workspace privacy settings. When an email is unavailable, the user's display name is used as their legacy identity.

**Start date custom field ID may differ.** If your Workspace A uses a different custom field ID for Start date, set `start_date_field` in `config.yaml` or the `START_DATE_FIELD` env var. You can find your field IDs at **Jira settings → Issues → Custom fields**.

---

## File structure

```
jira_migration/
├── migrate_project.py      # CLI entry point
├── config.py               # Config loading (env vars + YAML)
├── config.yaml.example     # Config template (copy to config.yaml)
├── extract.py              # Fetch issues and comments from Workspace A
├── transform.py            # Strategy A: raw issues → CSV row dicts
├── write_csv.py            # Strategy A: write CSV file
├── transform_rest.py       # Strategy B: raw issues → REST API payloads
├── write_rest.py           # Strategy B: create issues/comments in Workspace B
├── adf_utils.py            # ADF → plain text conversion (Strategy A)
├── user_mapping.py         # Load and apply user_mapping.csv
├── user_mapping.csv.example
├── requirements.txt
└── output/                 # Generated CSV files (gitignored)
```
