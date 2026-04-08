# jira_migration

A Python tool for migrating Jira projects between two Atlassian Cloud workspaces using the Jira REST API. Does not use JCMA or Atlassian's copy-plan tool.

---

## How it works

Two strategies are supported:

| Strategy | How | Best for |
|---|---|---|
| **A – CSV** | Exports issues to a CSV file for manual import via Jira's built-in CSV importer | Quick migrations, no dest credentials needed |
| **B – REST** | Creates issues, comments, and transitions directly in Workspace B via the REST API | Full fidelity: ADF rich text, comments, statuses, dates, sprints |

Both strategies extract from Workspace A, apply a user mapping, and preserve the original reporter/assignee identity in the issue description.

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
| Parent / child hierarchy | ✅ | ✅ topologically sorted (any depth) |
| Sprints | ❌ | ✅ created in dest board if missing |
| Comments | ❌ | ✅ with original author + date header |
| Attachments | ❌ | ⚠️ issue-level via `migrate_attachments.py`; inline ADF media not transferable |
| Source issue key | ✅ `IssueKey` column | ✅ prepended to description |
| Original reporter / assignee | ✅ extra columns or appended to description | ✅ always appended to description |

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
| `migration.legacy_info_strategy` | `LEGACY_INFO_STRATEGY` | `extra_columns` | How to record unmapped users in Strategy A: `extra_columns`, `append_description`, or `both`. Strategy B always appends to description. |
| `migration.unmapped_user_placeholder` | `UNMAPPED_USER_PLACEHOLDER` | `""` | Email to use for unmapped users in reporter/assignee fields. Leave blank to leave unassigned. |
| `migration.output_dir` | `OUTPUT_DIR` | `output` | Directory for Strategy A CSV files |
| `migration.page_size` | `PAGE_SIZE` | `100` | Issues per API page (Jira max: 100) |
| `migration.issue_type_map` | _(YAML only)_ | `{}` | Map issue type names from Workspace A → B, e.g. `Story: Task` |
| `migration.fallback_issue_type` | `FALLBACK_ISSUE_TYPE` | `Task` | Issue type to use when source type doesn't exist in Workspace B and has no mapping |
| `migration.start_date_field` | `START_DATE_FIELD` | `customfield_10015` | Custom field ID for "Start date" |
| `migration.sprint_field` | `SPRINT_FIELD` | `customfield_10020` | Custom field ID for "Sprint" |

---

## User mapping

Create `user_mapping.csv` (gitignored) to map source users to destination emails. The file has three columns:

```csv
source_email,source_account_id,target_email
alice@oldco.com,712020:abc123,alice@newco.com
bob@oldco.com,,bob@newco.com
```

- **`source_email`** — the user's email in Workspace A
- **`source_account_id`** — the user's Jira accountId in Workspace A (optional but recommended)
- **`target_email`** — the user's email in Workspace B

Both `source_email` and `source_account_id` are indexed as lookup keys. This is important because Jira Cloud often omits `emailAddress` from API responses due to workspace privacy settings — in those cases the accountId fallback is the only reliable match.

To find a user's accountId: navigate to their profile in Workspace A and copy the `accountId` query parameter from the URL.

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
2. **For Scrum projects: ensure an agile board exists** for the destination project. The script automatically finds the board, syncs existing sprints by name, and creates missing sprints. If no board is found, sprint assignment is skipped with a warning.
3. **Configure the workflow** — the script transitions issues to their source status by name. If Workspace B's workflow uses different status names, either update the workflow to match, or issues will be left in the default status (a warning is printed).
4. **Verify issue types** — the script fetches available types from the destination project and applies `issue_type_map` + `fallback_issue_type`. Check the startup output to confirm types are resolving correctly.

---

## Limitations and known behaviors

**No resume capability.** If the migration is interrupted and restarted, all issues are created again from scratch, resulting in duplicates. If this happens, delete the destination project's issues and re-run.

**Comments are always posted as the API token user.** Jira Cloud does not allow posting comments as another user via the REST API, regardless of permissions. Each comment is prepended with an italic header: _"Originally posted by [name] on [date]:"_

**Source issue key is always preserved.** Each migrated issue has an italic _"Migrated from: PROJ1-42"_ line prepended to its description.

**Original reporter and assignee are always recorded.** Every migrated issue appends an italic block to the description showing the original reporter and assignee from Workspace A — whether or not they were successfully mapped to Workspace B. This preserves provenance even after user accounts change.

**Unmapped users.** When a user in Workspace A has no entry in `user_mapping.csv` (by email or accountId), the reporter/assignee field is left blank (or set to `unmapped_user_placeholder`). Their original identity is still recorded in the description.

**Attachments at the issue level can be migrated separately.** Issue-level file attachments (including images pasted into comments, which Jira also attaches at the issue level) can be migrated using `migrate_attachments.py` after the main migration completes. However, inline images embedded inside ADF description or comment bodies via Jira's media service are workspace-specific and cannot be transferred — those are replaced with an italic _"[Attachment not migrated]"_ placeholder.

**Jira Cloud hides email addresses.** The API often omits `emailAddress` from user objects due to workspace privacy settings. The `source_account_id` column in `user_mapping.csv` provides a reliable fallback — populate it for users whose emails may be hidden.

**Sprints: closed sprints cannot be fully replicated.** The script creates missing sprints and attempts to match their state (active/closed/future). However, Jira Cloud enforces constraints on sprint state transitions — a sprint cannot be re-opened once closed, and only one sprint can be active at a time. Sprint names and dates are preserved; final state may differ from the source.

**Parent-child hierarchy is handled at any depth.** Issues are created in topological order (Epics before Stories, Stories before Tasks, Tasks before Subtasks) so parent links are always valid. This works regardless of issue type — it is not limited to formal Subtask types.

**Custom field IDs may differ.** The Start date and Sprint field IDs (`customfield_10015`, `customfield_10020`) are standard for most Jira Cloud instances but can vary. If your fields aren't migrating, find your field IDs at **Jira settings → Issues → Custom fields** and update `start_date_field` / `sprint_field` in `config.yaml`.

---

## Post-migration utilities

These standalone scripts run independently of `migrate_project.py` and are designed for use after the main migration completes. Each uses only the environment variables it needs.

### `download_attachments.py` — download attachments from Workspace A

Downloads issue-level attachments from Workspace A to a local directory. Useful for backup or as input to `migrate_attachments.py`.

```bash
# Download attachments for a single issue
python download_attachments.py --issue-key PROJ-123 --dir ./attachments

# Download all attachments for a project
python download_attachments.py --project PROJ --dir ./attachments
```

Requires: `JIRA_A_BASE_URL`, `JIRA_A_EMAIL`, `JIRA_A_API_TOKEN`

Files are saved to `<dir>/<issue-key>/<filename>`. Existing files are skipped.

---

### `migrate_attachments.py` — copy attachments from Workspace A → B

Iterates over issues in Workspace B, reads the _"Migrated from: XXXX-NNN"_ sentinel in each description to identify the source issue key, downloads attachments from Workspace A (caching them in `/tmp/jira_attachment_cache`), and uploads them to the corresponding Workspace B issue. Skips attachments that already exist by filename.

```bash
python migrate_attachments.py --project PROJ
```

Requires: `JIRA_A_BASE_URL`, `JIRA_A_EMAIL`, `JIRA_A_API_TOKEN`, `JIRA_B_BASE_URL`, `JIRA_B_EMAIL`, `JIRA_B_API_TOKEN`

Note: only issue-level attachments are migrated. Inline media embedded in ADF descriptions/comments cannot be transferred between workspaces.

---

### `backfill_user.py` — fix reporter/assignee for a specific unmapped user

If a user was not in `user_mapping.csv` during the original migration, their reporter/assignee fields will be blank. This script scans migrated issues for _"Original reporter: NAME"_ / _"Original assignee: NAME"_ in the description, resolves the target email to a Workspace B accountId, and updates the fields.

```bash
# Dry run (shows what would change)
python backfill_user.py --display-name "Erik MacLennan" --email erik@newco.com --project PROJ --dry-run

# Apply changes
python backfill_user.py --display-name "Erik MacLennan" --email erik@newco.com --project PROJ
```

Requires: `JIRA_B_BASE_URL`, `JIRA_B_EMAIL`, `JIRA_B_API_TOKEN` only (does not need Workspace A credentials).

---

### `migrate_deliverables.py` — migrate a Deliverables custom field to descriptions

Reads the `Deliverables` rich-text custom field (`customfield_10712`) from Workspace A issues and appends its content to the corresponding Workspace B issue descriptions. Uses a sentinel heading to avoid duplicate appends.

```bash
python migrate_deliverables.py --project PROJ
```

Requires: `JIRA_A_BASE_URL`, `JIRA_A_EMAIL`, `JIRA_A_API_TOKEN`, `JIRA_B_BASE_URL`, `JIRA_B_EMAIL`, `JIRA_B_API_TOKEN`

---

### `migrate_custom_fields.py` — migrate arbitrary custom fields via a JSON config

A config-driven script for migrating any set of custom fields from Workspace A to Workspace B. Each project has its own JSON config file that lists the source field ID, destination field ID, and field type for each field to migrate.

```bash
python migrate_custom_fields.py --config field_migration_config.HEG.json
```

Requires: `JIRA_A_BASE_URL`, `JIRA_A_EMAIL`, `JIRA_A_API_TOKEN`, `JIRA_B_BASE_URL`, `JIRA_B_EMAIL`, `JIRA_B_API_TOKEN`

**Supported field types:**

| Type | Behavior |
|---|---|
| `number` | Copies numeric value directly |
| `text` | Copies plain text value directly |
| `date` | Copies date string directly |
| `url` | Copies URL value directly |
| `select` | Copies the option value label |
| `adf` | Copies full ADF rich-text content to dest field |
| `adf_append` | Appends ADF content to the destination description with a sentinel heading |
| `people` | Resolves source accountId → email via `user_mapping.csv` → Workspace B accountId |

Fields are skipped if the destination field is already populated. If a `dest_field` is `null` or the type is `adf_append`, the content is appended to the description instead.

**Config file format** (`field_migration_config.example.json` has a full template):

```json
{
  "dest_project": "PROJKEY",
  "fields": [
    { "name": "Story points actual", "type": "number",  "source_field": "customfield_10016", "dest_field": "customfield_10028" },
    { "name": "Collaborators",       "type": "people",  "source_field": "customfield_10100", "dest_field": "customfield_10200" },
    { "name": "Notes",               "type": "adf",     "source_field": "customfield_10300", "dest_field": "customfield_10400" }
  ]
}
```

---

## File structure

```
jira_migration/
├── migrate_project.py              # CLI entry point (Strategy A & B)
├── config.py                       # Config loading (env vars + YAML)
├── config.yaml.example             # Config template (copy to config.yaml)
├── extract.py                      # Fetch issues and comments from Workspace A
├── transform.py                    # Strategy A: raw issues → CSV row dicts
├── write_csv.py                    # Strategy A: write CSV file
├── transform_rest.py               # Strategy B: raw issues → REST API payloads
├── write_rest.py                   # Strategy B: create issues/comments in Workspace B
├── adf_utils.py                    # ADF → plain text conversion (Strategy A)
├── user_mapping.py                 # Load and apply user_mapping.csv
│
├── download_attachments.py         # Download issue attachments from Workspace A
├── migrate_attachments.py          # Copy attachments from Workspace A → B
├── backfill_user.py                # Fix reporter/assignee for a missed unmapped user
├── migrate_deliverables.py         # Migrate Deliverables custom field to descriptions
├── migrate_custom_fields.py        # Config-driven migration of arbitrary custom fields
├── field_migration_config.example.json   # Template for custom field config
│
├── user_mapping.csv.example
├── requirements.txt
└── output/                         # Generated CSV files (gitignored)
```
