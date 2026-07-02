# Spotfire Report Validator

Automated validation of TIBCO Spotfire reports during **Teradata → BigQuery** migration. Compares data tables exported from as-is (Teradata) and to-be (BigQuery) reports using the Spotfire Server REST API + Automation Services — no local Spotfire Analyst installation required.

## Problem

During migration, the team must validate that BigQuery reports produce the same data as the original Teradata reports. Currently this is done by **manually opening both reports side-by-side** and comparing visuals — a process that takes hours per report (some reports take 2–3 hours just to load).

## Solution

This tool automates the validation pipeline:

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  1. Export       │ ──▶ │  2. Compare      │ ──▶ │  3. Report       │
│  (server-side    │     │  (pandas-based   │     │  (HTML diff      │
│   via Automation │     │   with type norm  │     │   + summary      │
│   Services)      │     │   + sort)         │     │   dashboard)     │
└──────────────────┘     └──────────────────┘     └──────────────────┘
```

1. **Export** — Triggers Automation Services jobs on the Spotfire server to export data tables to CSV (server-side, no interactive loading).
2. **Compare** — Loads both CSVs into pandas, normalizes types (timestamps, nulls, float precision), sorts rows, and compares row counts, column names, values, and checksums.
3. **Report** — Generates an HTML diff report per report pair + a summary dashboard.

### Key Features

- **Data-table-level comparison** — validates the actual report output (including Spotfire custom columns/measures), not just raw SQL.
- **Server-side export** — avoids the 2–3 hour interactive load time. Runs from any machine with network access to the Spotfire server.
- **Bookmark support** — applies the same bookmark to both reports for identical filter state.
- **Sort-before-compare** — handles non-deterministic row ordering from joins without `ORDER BY`.
- **Type normalization** — handles timestamp vs datetime, null representations, and float precision differences.
- **Configurable tolerances** — per-report row count tolerance, float precision, and null markers.
- **Lightweight & portable** — pure Python, only 4 dependencies (requests, pandas, pyyaml, jinja2).

## Quick Start

### Prerequisites

- Python 3.10+
- Network access to the Spotfire Server
- A Spotfire user account with Automation Services permissions
- Spotfire Server with Automation Services enabled and licensed

### Install

```bash
cd spotfire-validator
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

Edit `config.yaml`:

```yaml
spotfire:
  server_url: "https://spotfire.company.com/spotfire"
  api_base: "/api/rest"
  auth_type: "basic"
  username: "automation_user"
  password: ""  # Set via env: SPOTFIRE_PASSWORD

export:
  output_dir: "output/exports"
  max_rows: 10000       # Safety limit during validation
  format: "csv"

comparison:
  float_precision: 6
  row_count_tolerance_pct: 0.0
  max_diff_rows_display: 50

report_pairs:
  - name: "Cargo Revenue Report"
    as_is_dxp: "/Users/Cargo/Revenue_Report_TD"
    to_be_dxp: "/Users/Cargo/Revenue_Report_BQ"
    bookmark: "Q1_2026_Validation"
    data_tables: []  # empty = compare all
```

### Run

```bash
# Set password via environment variable
export SPOTFIRE_PASSWORD="your_password"

# Full pipeline: export → compare → report
python validator.py validate --config config.yaml

# Validate a single report pair
python validator.py validate --config config.yaml --pair "Cargo Revenue Report"

# Export only (no comparison — useful for pre-staging)
python validator.py export-only --config config.yaml

# Compare only (use pre-exported CSVs — no Spotfire server contact)
python validator.py compare-only --config config.yaml --pair "Cargo Revenue Report"

# List data tables in a DXP
python validator.py list-tables --config config.yaml --dxp /Users/Cargo/Revenue_Report_TD
```

### GUI

A sleek desktop GUI (`gui.py`) collects all configuration through a form, generates `config_gui.yaml`, runs the validation pipeline, and displays the output report path.

```bash
# Install GUI dependency
pip install PySide6

# Launch the GUI
python gui.py
```

The GUI:
- Pre-fills fields from `config.yaml` if present
- Lets you add/remove report pairs dynamically
- Runs validation in a background thread with live progress bar + log output
- On completion, shows the report output path with an "Open Report Folder" button

### Build Portable Windows EXE

PyInstaller bundles the GUI + all dependencies into a single `.exe` with no Python installation required on the target machine.

**On a Windows machine (or via GitHub Actions):**

```powershell
pip install -r requirements.txt pyinstaller
pyinstaller spotfire-validator-gui.spec --clean --noconfirm
# → dist/SpotfireValidator.exe
```

**Via GitHub Actions** (push a tag like `v1.0.0` or trigger manually):

```bash
git tag v1.0.0 && git push origin v1.0.0
# → Workflow builds the exe and attaches it to a GitHub Release
```

The resulting `SpotfireValidator.exe` is fully portable — double-click to run on any Windows 10/11 machine.

### Run Tests

```bash
pip install pytest
python -m pytest tests/ -v
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `validate` | Full pipeline: export data tables from both reports, compare, generate HTML report |
| `export-only` | Trigger Automation Services export jobs only (no comparison) |
| `compare-only` | Compare pre-exported CSVs only (no Spotfire server contact needed) |
| `list-tables` | List all data tables in a DXP |

## Output

```
output/
├── exports/
│   └── Cargo_Revenue_Report/
│       ├── as_is/
│       │   ├── SalesData.csv
│       │   └── CostSummary.csv
│       └── to_be/
│           ├── SalesData.csv
│           └── CostSummary.csv
└── reports/
    ├── Cargo_Revenue_Report.html   # Per-pair detailed report
    └── summary.html                 # Dashboard across all pairs
```

### HTML Report Contents

- **Summary cards**: tables compared, passed, failed, warnings, errors
- **Results table**: per-table status, row counts, column match, checksum match, mismatched columns
- **Collapsible details**: column differences, row set differences, sample mismatch rows (side-by-side)
- **Summary dashboard**: cross-pair overview with links to individual reports

## Architecture

```
spotfire-validator/
├── config.yaml              # Report pairs, server config, tolerances
├── validator.py             # Main CLI orchestrator (entry point)
├── gui.py                   # Sleek PySide6 GUI (entry point)
├── spotfire_client.py       # Spotfire Server REST API client
├── export_job_builder.py    # Automation Services job XML builder
├── comparator.py            # Data table comparison engine (pandas)
├── report_generator.py      # HTML report generator (Jinja2)
├── templates/
│   └── export_data_job.xml  # Automation Services job template
├── tests/
│   └── test_comparator.py   # Unit tests for comparison engine
├── spotfire-validator-gui.spec  # PyInstaller build spec
├── .github/workflows/
│   └── build-windows.yml    # CI: builds portable Windows exe
├── requirements.txt
└── README.md
```

## How It Addresses Meeting Problems

| Meeting Problem | How This Tool Helps |
|-----------------|---------------------|
| Manual side-by-side comparison | Automated CSV export + programmatic comparison |
| 2–3 hour report load time | Server-side export via Automation Services (no interactive loading) |
| Non-deterministic row ordering | Sort-before-compare with row-order flagging |
| Timestamp vs datetime mismatches | Type normalization to common format |
| Column name changes (spaces → underscores) | Column diff detection + sample mismatch display |
| No automated comparison tool | This tool is the automated comparison tool |
| Replication timing dependencies | `compare-only` mode lets you run validation whenever data is ready |

## Configuration Reference

### `spotfire` section

| Key | Default | Description |
|-----|---------|-------------|
| `server_url` | — | Spotfire Server URL (no trailing slash) |
| `api_base` | `/api/rest` | REST API base path |
| `auth_type` | `basic` | `basic` or `oauth` |
| `username` | — | Spotfire username |
| `password` | — | Password (or set `SPOTFIRE_PASSWORD` env var) |
| `timeout` | `120` | Request timeout in seconds |

### `export` section

| Key | Default | Description |
|-----|---------|-------------|
| `output_dir` | `output/exports` | Local directory for downloaded CSVs |
| `max_rows` | `10000` | Row limit per export (0 = no limit) |
| `format` | `csv` | Export format |
| `poll_interval` | `10` | Seconds between job status polls |
| `job_timeout` | `1800` | Max wait per job in seconds |

### `comparison` section

| Key | Default | Description |
|-----|---------|-------------|
| `sort_columns` | `[]` | Columns to sort by (empty = all columns) |
| `float_precision` | `6` | Decimal places for float comparison |
| `null_markers` | `["", "NULL", ...]` | Strings treated as NULL |
| `timestamp_format` | `%Y-%m-%d %H:%M:%S` | Normalized timestamp format |
| `row_count_tolerance_pct` | `0.0` | Allowed row count diff % (0 = exact) |
| `max_diff_rows_display` | `50` | Max diff rows in HTML report |

### `report_pairs` section

Each entry:

| Key | Required | Description |
|-----|----------|-------------|
| `name` | ✅ | Unique pair name |
| `as_is_dxp` | ✅ | Spotfire library path of Teradata report |
| `to_be_dxp` | ✅ | Spotfire library path of BigQuery report |
| `bookmark` | — | Bookmark to apply before export |
| `data_tables` | — | List of table names to compare (empty = all) |
| `overrides` | — | Per-pair comparison config overrides |

## Notes

- **Automation Services must be enabled** on the Spotfire Server. Check with your Spotfire admin.
- The REST API endpoints may vary slightly by Spotfire version. The client uses the v1 API (`/api/rest/api/v1/...`). Adjust `api_base` in config if your server uses a different path.
- For very large data tables (50M+ rows), use `max_rows: 10000` during iterative validation and remove the limit only for final sign-off.
- The `compare-only` command is useful when you want to re-compare without re-exporting (e.g., after adjusting tolerance settings).

## License

Internal use only.