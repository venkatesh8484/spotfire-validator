"""
Report Generator — HTML diff reports for Spotfire report validation.

Generates:
  1. Per-pair HTML report (one report pair = one as-is DXP + one to-be DXP)
  2. Summary dashboard across all report pairs

Uses Jinja2 templates rendered to static HTML — no web server needed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from comparator import TableComparisonResult

logger = logging.getLogger(__name__)

# Inline Jinja2 templates (no external template files needed for portability)

_PAIR_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Validation Report: {{ pair_name }}</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 2rem; background: #f8f9fa; }
  h1 { color: #1a73e8; }
  h2 { color: #333; border-bottom: 2px solid #e0e0e0; padding-bottom: 0.3rem; }
  .meta { background: #fff; padding: 1rem; border-radius: 8px; margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  .meta table { border-collapse: collapse; }
  .meta td { padding: 4px 12px; }
  .meta td:first-child { font-weight: 600; color: #555; }
  .summary { display: flex; gap: 1rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
  .card { background: #fff; padding: 1rem 1.5rem; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); min-width: 180px; }
  .card .label { font-size: 0.85rem; color: #666; text-transform: uppercase; }
  .card .value { font-size: 1.8rem; font-weight: 700; margin-top: 0.3rem; }
  .pass { color: #1e8e3e; }
  .fail { color: #d93025; }
  .warn { color: #f9ab00; }
  table.results { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  table.results th { background: #1a73e8; color: #fff; padding: 10px 12px; text-align: left; font-size: 0.9rem; }
  table.results td { padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 0.88rem; }
  table.results tr:hover { background: #f5f7ff; }
  .badge { padding: 3px 10px; border-radius: 12px; font-size: 0.8rem; font-weight: 600; }
  .badge-pass { background: #e6f4ea; color: #1e8e3e; }
  .badge-fail { background: #fce8e6; color: #d93025; }
  .badge-warn { background: #fef7e0; color: #f9ab00; }
  .badge-error { background: #f3e8fd; color: #9334e6; }
  .diff-table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
  .diff-table th { background: #f0f0f0; padding: 6px 10px; text-align: left; font-size: 0.82rem; }
  .diff-table td { padding: 6px 10px; border-bottom: 1px solid #eee; font-size: 0.82rem; font-family: monospace; }
  .diff-as-is { color: #d93025; }
  .diff-to-be { color: #1a73e8; }
  .collapsible { cursor: pointer; padding: 8px 12px; background: #f0f4ff; border-radius: 4px; margin-top: 0.5rem; font-size: 0.85rem; }
  .collapsible:hover { background: #e0e8ff; }
  .content { display: none; padding: 10px; border: 1px solid #e0e0e0; border-radius: 4px; margin-top: 4px; }
  .timestamp { color: #999; font-size: 0.85rem; }
</style>
</head>
<body>
  <h1>Validation Report: {{ pair_name }}</h1>
  <p class="timestamp">Generated: {{ generated_at }}</p>

  <div class="meta">
    <table>
      <tr><td>As-Is Report (Teradata):</td><td>{{ as_is_dxp }}</td></tr>
      <tr><td>To-Be Report (BigQuery):</td><td>{{ to_be_dxp }}</td></tr>
      <tr><td>Bookmark:</td><td>{{ bookmark or "(none — full data)" }}</td></tr>
      <tr><td>Overall Status:</td><td><span class="badge badge-{{ overall_status }}">{{ overall_status | upper }}</span></td></tr>
    </table>
  </div>

  <div class="summary">
    <div class="card"><div class="label">Tables Compared</div><div class="value">{{ results | length }}</div></div>
    <div class="card"><div class="label">Passed</div><div class="value pass">{{ pass_count }}</div></div>
    <div class="card"><div class="label">Failed</div><div class="value fail">{{ fail_count }}</div></div>
    <div class="card"><div class="label">Warnings</div><div class="value warn">{{ warn_count }}</div></div>
    <div class="card"><div class="label">Errors</div><div class="value">{{ error_count }}</div></div>
  </div>

  <h2>Data Table Results</h2>
  <table class="results">
    <thead>
      <tr>
        <th>Table Name</th>
        <th>Status</th>
        <th>Rows (As-Is)</th>
        <th>Rows (To-Be)</th>
        <th>Row Match</th>
        <th>Columns Match</th>
        <th>Checksum Match</th>
        <th>Mismatched Cols</th>
        <th>Elapsed (s)</th>
      </tr>
    </thead>
    <tbody>
      {% for r in results %}
      <tr>
        <td>{{ r.table_name }}</td>
        <td><span class="badge badge-{{ r.status }}">{{ r.status | upper }}</span></td>
        <td>{{ r.row_count_as_is }}</td>
        <td>{{ r.row_count_to_be }}</td>
        <td>{% if r.row_count_match %}✅{% else %}❌ ({{ "%.2f"|format(r.row_count_diff_pct) }}%){% endif %}</td>
        <td>{% if r.columns_match %}✅{% else %}❌{% endif %}</td>
        <td>{% if r.checksum_match %}✅{% else %}❌{% endif %}</td>
        <td>{{ r.column_diffs | selectattr('value_match', 'equalto', false) | list | length }}</td>
        <td>{{ "%.1f"|format(r.elapsed_seconds) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  {% for r in results %}
  {% if r.status != 'pass' %}
  <div class="collapsible" onclick="toggle('detail-{{ loop.index }}')">
    ▸ Details: {{ r.table_name }} — {{ r.status | upper }}
  </div>
  <div id="detail-{{ loop.index }}" class="content">
    {% if r.error %}
    <p style="color: #d93025;"><strong>Error:</strong> {{ r.error }}</p>
    {% endif %}

    {% if r.columns_only_in_as_is or r.columns_only_in_to_be %}
    <h3>Column Differences</h3>
    <p><strong>Only in As-Is:</strong> {{ r.columns_only_in_as_is | join(", ") or "(none)" }}</p>
    <p><strong>Only in To-Be:</strong> {{ r.columns_only_in_to_be | join(", ") or "(none)" }}</p>
    {% endif %}

    {% if r.rows_only_in_as_is > 0 or r.rows_only_in_to_be > 0 %}
    <h3>Row Set Differences</h3>
    <p>Rows only in As-Is: <strong>{{ r.rows_only_in_as_is }}</strong></p>
    <p>Rows only in To-Be: <strong>{{ r.rows_only_in_to_be }}</strong></p>
    {% endif %}

    {% if r.row_order_differs %}
    <p class="warn">⚠ Row ordering differs between as-is and to-be (sorted before comparison).</p>
    {% endif %}

    {% for cd in r.column_diffs if not cd.value_match %}
    <h4>Column: {{ cd.column_name }} ({{ cd.mismatch_count }} mismatches)</h4>
    <table class="diff-table">
      <thead><tr><th>Row #</th><th class="diff-as-is">As-Is (Teradata)</th><th class="diff-to-be">To-Be (BigQuery)</th></tr></thead>
      <tbody>
        {% for m in cd.sample_mismatches %}
        <tr><td>{{ m.row }}</td><td>{{ m.as_is }}</td><td>{{ m.to_be }}</td></tr>
        {% endfor %}
      </tbody>
    </table>
    {% endfor %}

    {% if r.sample_diff_rows %}
    <h3>Sample Diff Rows</h3>
    <table class="diff-table">
      <thead><tr><th>Row Index</th><th>Column</th><th class="diff-as-is">As-Is</th><th class="diff-to-be">To-Be</th></tr></thead>
      <tbody>
        {% for d in r.sample_diff_rows %}
        {% for col, vals in d.columns.items() %}
        <tr><td>{{ d.row_index }}</td><td>{{ col }}</td><td>{{ vals.as_is }}</td><td>{{ vals.to_be }}</td></tr>
        {% endfor %}
        {% endfor %}
      </tbody>
    </table>
    {% endif %}
  </div>
  {% endif %}
  {% endfor %}
</div>

<script>
function toggle(id) {
  var el = document.getElementById(id);
  el.style.display = el.style.display === 'block' ? 'none' : 'block';
}
</script>
</body>
</html>"""

_SUMMARY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Spotfire Validation Summary</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 2rem; background: #f8f9fa; }
  h1 { color: #1a73e8; }
  .summary { display: flex; gap: 1rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
  .card { background: #fff; padding: 1rem 1.5rem; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); min-width: 180px; }
  .card .label { font-size: 0.85rem; color: #666; text-transform: uppercase; }
  .card .value { font-size: 1.8rem; font-weight: 700; margin-top: 0.3rem; }
  .pass { color: #1e8e3e; }
  .fail { color: #d93025; }
  .warn { color: #f9ab00; }
  table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  th { background: #1a73e8; color: #fff; padding: 10px 12px; text-align: left; }
  td { padding: 8px 12px; border-bottom: 1px solid #eee; }
  tr:hover { background: #f5f7ff; }
  .badge { padding: 3px 10px; border-radius: 12px; font-size: 0.8rem; font-weight: 600; }
  .badge-pass { background: #e6f4ea; color: #1e8e3e; }
  .badge-fail { background: #fce8e6; color: #d93025; }
  .badge-warn { background: #fef7e0; color: #f9ab00; }
  .badge-error { background: #f3e8fd; color: #9334e6; }
  .timestamp { color: #999; font-size: 0.85rem; }
  a { color: #1a73e8; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>
  <h1>Spotfire Validation Summary</h1>
  <p class="timestamp">Generated: {{ generated_at }}</p>

  <div class="summary">
    <div class="card"><div class="label">Report Pairs</div><div class="value">{{ pairs | length }}</div></div>
    <div class="card"><div class="label">Passed</div><div class="value pass">{{ total_pass }}</div></div>
    <div class="card"><div class="label">Failed</div><div class="value fail">{{ total_fail }}</div></div>
    <div class="card"><div class="label">Warnings</div><div class="value warn">{{ total_warn }}</div></div>
    <div class="card"><div class="label">Errors</div><div class="value">{{ total_error }}</div></div>
  </div>

  <table>
    <thead>
      <tr>
        <th>Report Pair</th>
        <th>Status</th>
        <th>Tables Compared</th>
        <th>Tables Passed</th>
        <th>Tables Failed</th>
        <th>Elapsed (s)</th>
        <th>Report Link</th>
      </tr>
    </thead>
    <tbody>
      {% for p in pairs %}
      <tr>
        <td>{{ p.name }}</td>
        <td><span class="badge badge-{{ p.overall_status }}">{{ p.overall_status | upper }}</span></td>
        <td>{{ p.table_count }}</td>
        <td class="pass">{{ p.pass_count }}</td>
        <td class="fail">{{ p.fail_count }}</td>
        <td>{{ "%.1f"|format(p.elapsed_seconds) }}</td>
        <td><a href="{{ p.report_file }}">View Report</a></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
#  ReportGenerator
# ═══════════════════════════════════════════════════════════════

class ReportGenerator:
    """Generates HTML validation reports."""

    def __init__(self, output_dir: str):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._env = Environment(autoescape=select_autoescape(["html"]))

    def generate_pair_report(
        self,
        pair_name: str,
        as_is_dxp: str,
        to_be_dxp: str,
        bookmark: str,
        results: list[TableComparisonResult],
    ) -> str:
        """
        Generate an HTML report for a single report pair.

        Returns the output file path.
        """
        template = self._env.from_string(_PAIR_TEMPLATE)

        overall_status = self._overall_status(results)
        pass_count = sum(1 for r in results if r.status == "pass")
        fail_count = sum(1 for r in results if r.status == "fail")
        warn_count = sum(1 for r in results if r.status == "warn")
        error_count = sum(1 for r in results if r.status == "error")

        html = template.render(
            pair_name=pair_name,
            as_is_dxp=as_is_dxp,
            to_be_dxp=to_be_dxp,
            bookmark=bookmark or "",
            overall_status=overall_status,
            results=results,
            pass_count=pass_count,
            fail_count=fail_count,
            warn_count=warn_count,
            error_count=error_count,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        safe_name = pair_name.replace(" ", "_").replace("/", "_")
        out_file = self.output_dir / f"{safe_name}.html"
        out_file.write_text(html, encoding="utf-8")
        logger.info("Pair report written to %s", out_file)
        return str(out_file)

    def generate_summary(
        self, pair_summaries: list[dict[str, Any]]
    ) -> str:
        """
        Generate a summary dashboard across all report pairs.

        ``pair_summaries`` is a list of dicts with keys:
          name, overall_status, table_count, pass_count, fail_count,
          elapsed_seconds, report_file
        """
        template = self._env.from_string(_SUMMARY_TEMPLATE)

        total_pass = sum(1 for p in pair_summaries if p["overall_status"] == "pass")
        total_fail = sum(1 for p in pair_summaries if p["overall_status"] == "fail")
        total_warn = sum(1 for p in pair_summaries if p["overall_status"] == "warn")
        total_error = sum(1 for p in pair_summaries if p["overall_status"] == "error")

        html = template.render(
            pairs=pair_summaries,
            total_pass=total_pass,
            total_fail=total_fail,
            total_warn=total_warn,
            total_error=total_error,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        out_file = self.output_dir / "summary.html"
        out_file.write_text(html, encoding="utf-8")
        logger.info("Summary report written to %s", out_file)
        return str(out_file)

    @staticmethod
    def _overall_status(results: list[TableComparisonResult]) -> str:
        """Determine overall status from a list of table results."""
        if any(r.status == "error" for r in results):
            return "error"
        if any(r.status == "fail" for r in results):
            return "fail"
        if any(r.status == "warn" for r in results):
            return "warn"
        return "pass"