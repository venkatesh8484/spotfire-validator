#!/usr/bin/env python3
"""
Spotfire Report Validator — CLI entry point.

Automates validation of Spotfire reports during Teradata → BigQuery migration.
Compares data tables exported from as-is (Teradata) and to-be (BigQuery) reports
using the Spotfire Server REST API + Automation Services.

Usage:
  python validator.py validate      --config config.yaml [--pair NAME]
  python validator.py export-only   --config config.yaml [--pair NAME]
  python validator.py compare-only   --config config.yaml --pair NAME
  python validator.py list-tables   --config config.yaml --dxp PATH

Phases:
  1. Export: Trigger Automation Services jobs to export data tables to CSV
  2. Compare: Load CSVs and compare with type normalization + sorting
  3. Report: Generate HTML diff reports

Reuses CLI patterns from ``bo-bq-migrator/migrate.py``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# Add current directory to path for direct script execution
sys.path.insert(0, str(Path(__file__).parent))

from comparator import ComparisonConfig, DataComparator, TableComparisonResult
from export_job_builder import build_export_job
from report_generator import ReportGenerator
from spotfire_client import SpotfireClient, SpotfireConfig, load_config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  Logging setup
# ═══════════════════════════════════════════════════════════════

def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ═══════════════════════════════════════════════════════════════
#  Config helpers
# ═══════════════════════════════════════════════════════════════

def get_report_pairs(config: dict[str, Any], pair_name: str | None) -> list[dict]:
    """Get report pairs from config, optionally filtered by name."""
    pairs = config.get("report_pairs", [])
    if pair_name:
        pairs = [p for p in pairs if p["name"] == pair_name]
        if not pairs:
            raise ValueError(f"Report pair not found: {pair_name}")
    return pairs


def get_comparison_config(
    base: dict[str, Any], overrides: dict[str, Any] | None = None
) -> ComparisonConfig:
    """Build ComparisonConfig from base config + optional per-pair overrides."""
    data = dict(base.get("comparison", {}))
    if overrides:
        data.update(overrides)
    return ComparisonConfig.from_dict(data)


# ═══════════════════════════════════════════════════════════════
#  Phase 1: Export
# ═══════════════════════════════════════════════════════════════

def export_report_data(
    client: SpotfireClient,
    dxp_path: str,
    output_dir: str,
    bookmark: str,
    data_tables: list[str],
    max_rows: int,
    poll_interval: int,
    job_timeout: int,
    pair_name: str,
    side: str,  # "as_is" | "to_be"
) -> list[str]:
    """
    Export data tables from a DXP via Automation Services.

    Returns a list of local CSV file paths.
    """
    export_cfg = _get_export_config()
    server_output_dir = f"/tmp/spotfire_validator/{pair_name}/{side}"
    local_dir = Path(output_dir) / pair_name.replace(" ", "_") / side
    local_dir.mkdir(parents=True, exist_ok=True)

    # Determine which data tables to export
    if not data_tables:
        # List all data tables in the DXP
        item = client.find_library_item(dxp_path)
        tables = client.list_data_tables(item.item_id)
        table_names = [t["name"] for t in tables]
        if not table_names:
            logger.warning("No data tables found in %s", dxp_path)
            return []
        logger.info("Found %d data tables in %s: %s",
                    len(table_names), dxp_path, table_names)
    else:
        table_names = data_tables

    exported_files: list[str] = []

    for table_name in table_names:
        safe_name = table_name.replace(" ", "_").replace("/", "_")
        output_file = f"{safe_name}.csv"

        logger.info("Exporting table '%s' from %s …", table_name, dxp_path)

        job_xml = build_export_job(
            analysis_path=dxp_path,
            output_dir=server_output_dir,
            output_file=output_file,
            data_table_name=table_name,
            bookmark=bookmark,
            max_rows=max_rows,
        )

        try:
            result = client.execute_automation_job(
                job_xml, poll_interval=poll_interval, job_timeout=job_timeout
            )
        except Exception as e:
            logger.error("Export job failed for table '%s': %s", table_name, e)
            continue

        if result.status != "SUCCESS":
            logger.error(
                "Export job for '%s' returned status %s: %s",
                table_name, result.status, result.message,
            )
            continue

        # Download the exported CSV
        # The output file should be in the server library or a known location.
        # In practice, Automation Services writes to a server path; we download
        # via the library API or a file download endpoint.
        for output_file_id in result.output_files:
            local_path = str(local_dir / output_file)
            try:
                client.download_library_file(output_file_id, local_path)
                exported_files.append(local_path)
            except Exception as e:
                logger.error("Failed to download %s: %s", output_file_id, e)

    return exported_files


def _get_export_config() -> dict[str, Any]:
    """Placeholder for export config — populated by caller."""
    return {}


# ═══════════════════════════════════════════════════════════════
#  Phase 2: Compare
# ═══════════════════════════════════════════════════════════════

def compare_exported_data(
    as_is_dir: str,
    to_be_dir: str,
    comparison_config: ComparisonConfig,
    table_names: list[str] | None = None,
) -> list[TableComparisonResult]:
    """
    Compare CSVs in two directories.

    Matches files by name (case-insensitive).
    """
    comparator = DataComparator(comparison_config)
    results: list[TableComparisonResult] = []

    as_is_path = Path(as_is_dir)
    to_be_path = Path(to_be_dir)

    if not as_is_path.exists():
        logger.error("As-is export directory not found: %s", as_is_dir)
        return results
    if not to_be_path.exists():
        logger.error("To-be export directory not found: %s", to_be_dir)
        return results

    # Build file mapping
    as_is_files = {f.stem.lower(): f for f in as_is_path.glob("*.csv")}
    to_be_files = {f.stem.lower(): f for f in to_be_path.glob("*.csv")}

    all_names = set(as_is_files.keys()) | set(to_be_files.keys())
    if table_names:
        table_names_lower = [t.lower() for t in table_names]
        all_names = {n for n in all_names if n in table_names_lower}

    for name in sorted(all_names):
        as_is_csv = as_is_files.get(name)
        to_be_csv = to_be_files.get(name)

        if not as_is_csv:
            logger.warning("No as-is CSV for table '%s'", name)
            result = TableComparisonResult(table_name=name)
            result.status = "error"
            result.error = "No as-is CSV found"
            results.append(result)
            continue

        if not to_be_csv:
            logger.warning("No to-be CSV for table '%s'", name)
            result = TableComparisonResult(table_name=name)
            result.status = "error"
            result.error = "No to-be CSV found"
            results.append(result)
            continue

        logger.info("Comparing table '%s' …", name)
        result = comparator.compare_csvs(
            str(as_is_csv), str(to_be_csv), table_name=name
        )
        results.append(result)

        if result.status == "pass":
            logger.info("  ✅ PASS: %s", name)
        else:
            logger.warning("  ❌ %s: %s", result.status.upper(), name)

    return results


# ═══════════════════════════════════════════════════════════════
#  Phase 3: Report
# ═══════════════════════════════════════════════════════════════

def generate_reports(
    pair_name: str,
    as_is_dxp: str,
    to_be_dxp: str,
    bookmark: str,
    results: list[TableComparisonResult],
    report_dir: str,
) -> str:
    """Generate HTML report for a pair. Returns the report file path."""
    gen = ReportGenerator(report_dir)
    return gen.generate_pair_report(
        pair_name=pair_name,
        as_is_dxp=as_is_dxp,
        to_be_dxp=to_be_dxp,
        bookmark=bookmark,
        results=results,
    )


# ═══════════════════════════════════════════════════════════════
#  CLI Commands
# ═══════════════════════════════════════════════════════════════

def cmd_validate(args: argparse.Namespace) -> int:
    """Full pipeline: export → compare → report."""
    config = load_config(args.config)
    setup_logging(args.verbose)

    spotfire_cfg = SpotfireConfig.from_dict(config.get("spotfire", {}))
    export_cfg = config.get("export", {})
    report_cfg = config.get("report", {})

    pairs = get_report_pairs(config, args.pair)
    if not pairs:
        logger.error("No report pairs found in config")
        return 1

    pair_summaries: list[dict[str, Any]] = []
    overall_exit_code = 0

    with SpotfireClient(spotfire_cfg) as client:
        for pair in pairs:
            pair_name = pair["name"]
            logger.info("=" * 60)
            logger.info("Processing pair: %s", pair_name)
            logger.info("=" * 60)

            overrides = pair.get("overrides", {})
            comp_cfg = get_comparison_config(config, overrides)

            # Phase 1: Export
            start = time.time()
            as_is_files = export_report_data(
                client=client,
                dxp_path=pair["as_is_dxp"],
                output_dir=export_cfg.get("output_dir", "output/exports"),
                bookmark=pair.get("bookmark", ""),
                data_tables=pair.get("data_tables", []),
                max_rows=export_cfg.get("max_rows", 10000),
                poll_interval=export_cfg.get("poll_interval", 10),
                job_timeout=export_cfg.get("job_timeout", 1800),
                pair_name=pair_name,
                side="as_is",
            )

            to_be_files = export_report_data(
                client=client,
                dxp_path=pair["to_be_dxp"],
                output_dir=export_cfg.get("output_dir", "output/exports"),
                bookmark=pair.get("bookmark", ""),
                data_tables=pair.get("data_tables", []),
                max_rows=export_cfg.get("max_rows", 10000),
                poll_interval=export_cfg.get("poll_interval", 10),
                job_timeout=export_cfg.get("job_timeout", 1800),
                pair_name=pair_name,
                side="to_be",
            )

            # Phase 2: Compare
            as_is_dir = str(
                Path(export_cfg.get("output_dir", "output/exports"))
                / pair_name.replace(" ", "_") / "as_is"
            )
            to_be_dir = str(
                Path(export_cfg.get("output_dir", "output/exports"))
                / pair_name.replace(" ", "_") / "to_be"
            )

            results = compare_exported_data(
                as_is_dir=as_is_dir,
                to_be_dir=to_be_dir,
                comparison_config=comp_cfg,
                table_names=pair.get("data_tables", []) or None,
            )

            elapsed = time.time() - start

            # Phase 3: Report
            report_file = generate_reports(
                pair_name=pair_name,
                as_is_dxp=pair["as_is_dxp"],
                to_be_dxp=pair["to_be_dxp"],
                bookmark=pair.get("bookmark", ""),
                results=results,
                report_dir=report_cfg.get("output_dir", "output/reports"),
            )

            # Summary entry
            pass_count = sum(1 for r in results if r.status == "pass")
            fail_count = sum(1 for r in results if r.status == "fail")
            overall = ReportGenerator._overall_status(results)
            if overall in ("fail", "error"):
                overall_exit_code = 1

            pair_summaries.append(
                {
                    "name": pair_name,
                    "overall_status": overall,
                    "table_count": len(results),
                    "pass_count": pass_count,
                    "fail_count": fail_count,
                    "elapsed_seconds": elapsed,
                    "report_file": Path(report_file).name,
                }
            )

    # Generate summary dashboard
    if pair_summaries:
        gen = ReportGenerator(report_cfg.get("output_dir", "output/reports"))
        summary_file = gen.generate_summary(pair_summaries)
        logger.info("Summary dashboard: %s", summary_file)

    logger.info("Done. Exit code: %d", overall_exit_code)
    return overall_exit_code


def cmd_export_only(args: argparse.Namespace) -> int:
    """Export only — trigger Automation Services jobs without comparing."""
    config = load_config(args.config)
    setup_logging(args.verbose)

    spotfire_cfg = SpotfireConfig.from_dict(config.get("spotfire", {}))
    export_cfg = config.get("export", {})

    pairs = get_report_pairs(config, args.pair)

    with SpotfireClient(spotfire_cfg) as client:
        for pair in pairs:
            pair_name = pair["name"]
            logger.info("Exporting as-is: %s", pair_name)
            export_report_data(
                client=client,
                dxp_path=pair["as_is_dxp"],
                output_dir=export_cfg.get("output_dir", "output/exports"),
                bookmark=pair.get("bookmark", ""),
                data_tables=pair.get("data_tables", []),
                max_rows=export_cfg.get("max_rows", 10000),
                poll_interval=export_cfg.get("poll_interval", 10),
                job_timeout=export_cfg.get("job_timeout", 1800),
                pair_name=pair_name,
                side="as_is",
            )

            logger.info("Exporting to-be: %s", pair_name)
            export_report_data(
                client=client,
                dxp_path=pair["to_be_dxp"],
                output_dir=export_cfg.get("output_dir", "output/exports"),
                bookmark=pair.get("bookmark", ""),
                data_tables=pair.get("data_tables", []),
                max_rows=export_cfg.get("max_rows", 10000),
                poll_interval=export_cfg.get("poll_interval", 10),
                job_timeout=export_cfg.get("job_timeout", 1800),
                pair_name=pair_name,
                side="to_be",
            )

    logger.info("Export complete.")
    return 0


def cmd_compare_only(args: argparse.Namespace) -> int:
    """Compare only — compare pre-exported CSVs without contacting Spotfire."""
    config = load_config(args.config)
    setup_logging(args.verbose)

    export_cfg = config.get("export", {})
    report_cfg = config.get("report", {})

    pairs = get_report_pairs(config, args.pair)

    pair_summaries: list[dict[str, Any]] = []
    overall_exit_code = 0

    for pair in pairs:
        pair_name = pair["name"]
        overrides = pair.get("overrides", {})
        comp_cfg = get_comparison_config(config, overrides)

        as_is_dir = str(
            Path(export_cfg.get("output_dir", "output/exports"))
            / pair_name.replace(" ", "_") / "as_is"
        )
        to_be_dir = str(
            Path(export_cfg.get("output_dir", "output/exports"))
            / pair_name.replace(" ", "_") / "to_be"
        )

        start = time.time()
        results = compare_exported_data(
            as_is_dir=as_is_dir,
            to_be_dir=to_be_dir,
            comparison_config=comp_cfg,
            table_names=pair.get("data_tables", []) or None,
        )
        elapsed = time.time() - start

        report_file = generate_reports(
            pair_name=pair_name,
            as_is_dxp=pair["as_is_dxp"],
            to_be_dxp=pair["to_be_dxp"],
            bookmark=pair.get("bookmark", ""),
            results=results,
            report_dir=report_cfg.get("output_dir", "output/reports"),
        )

        pass_count = sum(1 for r in results if r.status == "pass")
        fail_count = sum(1 for r in results if r.status == "fail")
        overall = ReportGenerator._overall_status(results)
        if overall in ("fail", "error"):
            overall_exit_code = 1

        pair_summaries.append(
            {
                "name": pair_name,
                "overall_status": overall,
                "table_count": len(results),
                "pass_count": pass_count,
                "fail_count": fail_count,
                "elapsed_seconds": elapsed,
                "report_file": Path(report_file).name,
            }
        )

    if pair_summaries:
        gen = ReportGenerator(report_cfg.get("output_dir", "output/reports"))
        gen.generate_summary(pair_summaries)

    logger.info("Done. Exit code: %d", overall_exit_code)
    return overall_exit_code


def cmd_list_tables(args: argparse.Namespace) -> int:
    """List data tables in a DXP."""
    config = load_config(args.config)
    setup_logging(args.verbose)

    spotfire_cfg = SpotfireConfig.from_dict(config.get("spotfire", {}))

    with SpotfireClient(spotfire_cfg) as client:
        item = client.find_library_item(args.dxp)
        tables = client.list_data_tables(item.item_id)

        print(f"\nData tables in '{args.dxp}':")
        print("-" * 60)
        for t in tables:
            print(f"  {t['name']:40s}  ({t.get('type', 'unknown')})")
        print("-" * 60)
        print(f"Total: {len(tables)} table(s)")

    return 0


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Spotfire Report Validator — Teradata → BigQuery migration validation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  validate      Full pipeline: export data tables → compare → generate report
  export-only   Trigger Automation Services export jobs (no comparison)
  compare-only  Compare pre-exported CSVs (no Spotfire server contact)
  list-tables   List data tables in a DXP

Examples:
  python validator.py validate --config config.yaml
  python validator.py validate --config config.yaml --pair "Cargo Revenue Report"
  python validator.py compare-only --config config.yaml --pair "Cargo Revenue Report"
  python validator.py list-tables --config config.yaml --dxp /Users/Cargo/Revenue_Report_TD
""",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    # validate
    p_validate = sub.add_parser("validate", help="Full pipeline: export → compare → report")
    p_validate.add_argument("--config", required=True, help="Path to config.yaml")
    p_validate.add_argument("--pair", default=None, help="Process only this pair name")

    # export-only
    p_export = sub.add_parser("export-only", help="Export data tables only")
    p_export.add_argument("--config", required=True, help="Path to config.yaml")
    p_export.add_argument("--pair", default=None, help="Process only this pair name")

    # compare-only
    p_compare = sub.add_parser("compare-only", help="Compare pre-exported CSVs only")
    p_compare.add_argument("--config", required=True, help="Path to config.yaml")
    p_compare.add_argument("--pair", default=None, help="Process only this pair name")

    # list-tables
    p_list = sub.add_parser("list-tables", help="List data tables in a DXP")
    p_list.add_argument("--config", required=True, help="Path to config.yaml")
    p_list.add_argument("--dxp", required=True, help="Spotfire library path of the DXP")

    args = parser.parse_args()

    if args.command == "validate":
        sys.exit(cmd_validate(args))
    elif args.command == "export-only":
        sys.exit(cmd_export_only(args))
    elif args.command == "compare-only":
        sys.exit(cmd_compare_only(args))
    elif args.command == "list-tables":
        sys.exit(cmd_list_tables(args))


if __name__ == "__main__":
    main()