"""
Export Job Builder — generates Automation Services job XML.

Builds a job that opens a DXP, optionally applies a bookmark, and
exports one or all data tables to CSV.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "export_data_job.xml"


def build_export_job(
    analysis_path: str,
    output_dir: str,
    output_file: str,
    data_table_name: str = "*",
    bookmark: str = "",
    max_rows: int = 10000,
) -> str:
    """
    Build an Automation Services job XML string.

    Parameters
    ----------
    analysis_path
        Spotfire library path of the DXP (e.g. ``/Users/Cargo/Revenue_Report_TD``).
    output_dir
        Server-side output directory for the CSV.
    output_file
        Output CSV file name (without directory).
    data_table_name
        Name of the data table to export, or ``"*"`` for all tables.
    bookmark
        Bookmark name to apply before export (empty = no bookmark).
    max_rows
        Row limit for the export (0 = no limit).

    Returns
    -------
    str
        Complete Automation Services job XML, ready to POST.
    """
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")

    job_id = str(uuid.uuid4())

    # Build bookmark task block (or empty string)
    if bookmark:
        bookmark_task = (
            "    <task>\n"
            "      <id>setBookmark</id>\n"
            "      <type>SetBookmark</type>\n"
            "      <properties>\n"
            f"        <bookmarkName>{_xml_escape(bookmark)}</bookmarkName>\n"
            "      </properties>\n"
            "    </task>"
        )
    else:
        bookmark_task = ""

    xml = template.replace("{{JOB_ID}}", job_id)
    xml = xml.replace("{{ANALYSIS_PATH}}", _xml_escape(analysis_path))
    xml = xml.replace("{{BOOKMARK_NAME}}", _xml_escape(bookmark))
    xml = xml.replace("{{BOOKMARK_TASK}}", bookmark_task)
    xml = xml.replace("{{DATA_TABLE_NAME}}", _xml_escape(data_table_name))
    xml = xml.replace("{{OUTPUT_DIR}}", _xml_escape(output_dir))
    xml = xml.replace("{{OUTPUT_FILE}}", _xml_escape(output_file))
    xml = xml.replace("{{MAX_ROWS}}", str(max_rows))

    logger.debug("Built export job for %s (table=%s, bookmark=%s)",
                 analysis_path, data_table_name, bookmark or "(none)")
    return xml


def _xml_escape(s: str) -> str:
    """Minimal XML escaping."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )