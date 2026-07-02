#!/usr/bin/env python3
"""
Spotfire Report Validator — Sleek GUI

A minimalistic desktop GUI that collects configuration from the user,
generates a config.yaml, runs the validation pipeline, and displays
the output report path.

Built with PySide6 (Qt for Python). Packages into a portable .exe via PyInstaller.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

import yaml
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

# Ensure local imports work when running as script or frozen exe
BASE_DIR = Path(__file__).resolve().parent if not getattr(sys, "frozen", False) else Path(sys._MEIPASS)
sys.path.insert(0, str(BASE_DIR))

logger = logging.getLogger("spotfire-gui")


# ═══════════════════════════════════════════════════════════════
#  Stylesheet — sleek, minimalistic dark-accent theme
# ═══════════════════════════════════════════════════════════════

STYLESHEET = """
QMainWindow, QWidget {
    background: #f5f6f8;
    font-family: -apple-system, "SF Pro Display", "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 13px;
    color: #1d1d1f;
}
QGroupBox {
    font-weight: 600;
    font-size: 13px;
    color: #1a73e8;
    border: 1px solid #dcdce1;
    border-radius: 10px;
    margin-top: 14px;
    padding: 18px 14px 10px 14px;
    background: #ffffff;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top-left;
    padding: 0 8px;
    background: #f5f6f8;
}
QLabel {
    color: #3c4043;
}
QLineEdit, QSpinBox, QComboBox {
    padding: 6px 10px;
    border: 1px solid #dcdce1;
    border-radius: 6px;
    background: #ffffff;
    selection-background-color: #1a73e8;
    selection-color: #ffffff;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1.5px solid #1a73e8;
}
QPushButton {
    padding: 8px 22px;
    border: none;
    border-radius: 8px;
    font-weight: 600;
    font-size: 13px;
}
QPushButton#primary {
    background: #1a73e8;
    color: #ffffff;
}
QPushButton#primary:hover {
    background: #1557b0;
}
QPushButton#primary:disabled {
    background: #c4d4f0;
}
QPushButton#secondary {
    background: #e8eaed;
    color: #3c4043;
}
QPushButton#secondary:hover {
    background: #d2d5db;
}
QPlainTextEdit {
    background: #1e1e2e;
    color: #cdd6f4;
    border: 1px solid #dcdce1;
    border-radius: 8px;
    font-family: "SF Mono", "JetBrains Mono", "Fira Code", Menlo, Consolas, monospace;
    font-size: 12px;
    padding: 8px;
}
QProgressBar {
    border: none;
    border-radius: 6px;
    background: #e8eaed;
    height: 8px;
    text-align: center;
    font-size: 11px;
    color: #1d1d1f;
}
QProgressBar::chunk {
    border-radius: 6px;
    background: #1a73e8;
}
QScrollBar:vertical {
    border: none;
    background: #f5f6f8;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #c4c7cc;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #9aa0a6;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
"""


# ═══════════════════════════════════════════════════════════════
#  Validation worker thread
# ═══════════════════════════════════════════════════════════════

class ValidationWorker(QThread):
    """Runs the validation pipeline in a background thread."""

    log_signal = Signal(str)
    progress_signal = Signal(int, str)  # (percentage, status_text)
    finished_signal = Signal(bool, str)  # (success, report_path_or_error)

    def __init__(self, config_data: dict[str, Any], config_path: Path):
        super().__init__()
        self.config_data = config_data
        self.config_path = config_path

    def run(self) -> None:
        try:
            # Write config.yaml
            self.log_signal.emit("Writing configuration…")
            with open(self.config_path, "w") as f:
                yaml.dump(self.config_data, f, default_flow_style=False, sort_keys=False)
            self.log_signal.emit(f"Config written: {self.config_path}")

            # Import validator modules (deferred so GUI starts fast)
            self.progress_signal.emit(5, "Loading validator modules…")
            from comparator import ComparisonConfig, DataComparator, TableComparisonResult
            from export_job_builder import build_export_job
            from report_generator import ReportGenerator
            from spotfire_client import SpotfireClient, SpotfireConfig, load_config

            # Set up logging to capture in GUI
            root_logger = logging.getLogger()
            root_logger.setLevel(logging.INFO)
            handler = GuiLogHandler(self.log_signal)
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
            root_logger.addHandler(handler)

            config = load_config(str(self.config_path))
            spotfire_cfg = SpotfireConfig.from_dict(config.get("spotfire", {}))
            export_cfg = config.get("export", {})
            report_cfg = config.get("report", {})

            pairs = config.get("report_pairs", [])
            if not pairs:
                self.finished_signal.emit(False, "No report pairs defined.")
                return

            total_pairs = len(pairs)
            pair_summaries: list[dict[str, Any]] = []
            overall_exit_code = 0
            last_report_dir = report_cfg.get("output_dir", "output/reports")

            self.progress_signal.emit(10, "Connecting to Spotfire server…")

            with SpotfireClient(spotfire_cfg) as client:
                for idx, pair in enumerate(pairs):
                    pair_name = pair["name"]
                    base_pct = 10 + int(80 * idx / total_pairs)
                    pair_pct = int(80 / total_pairs)

                    self.progress_signal.emit(base_pct, f"Processing: {pair_name}")
                    self.log_signal.emit("=" * 50)
                    self.log_signal.emit(f"Processing pair: {pair_name}")
                    self.log_signal.emit("=" * 50)

                    overrides = pair.get("overrides", {})
                    comp_cfg = ComparisonConfig.from_dict(
                        {**config.get("comparison", {}), **overrides}
                    )

                    # Phase 1: Export as-is
                    self.progress_signal.emit(base_pct + pair_pct // 4, f"Exporting as-is: {pair_name}")
                    self._export_pair(client, pair, export_cfg, "as_is")

                    # Phase 1: Export to-be
                    self.progress_signal.emit(base_pct + pair_pct // 2, f"Exporting to-be: {pair_name}")
                    self._export_pair(client, pair, export_cfg, "to_be")

                    # Phase 2: Compare
                    self.progress_signal.emit(base_pct + int(pair_pct * 0.75), f"Comparing: {pair_name}")
                    as_is_dir = str(Path(export_cfg.get("output_dir", "output/exports")) / pair_name.replace(" ", "_") / "as_is")
                    to_be_dir = str(Path(export_cfg.get("output_dir", "output/exports")) / pair_name.replace(" ", "_") / "to_be")

                    results = self._compare(as_is_dir, to_be_dir, comp_cfg, pair.get("data_tables", []) or None)

                    # Phase 3: Report
                    self.progress_signal.emit(base_pct + pair_pct - 1, f"Generating report: {pair_name}")
                    report_file = self._generate_report(pair, results, report_cfg)
                    last_report_dir = str(Path(report_file).parent)

                    pass_count = sum(1 for r in results if r.status == "pass")
                    fail_count = sum(1 for r in results if r.status == "fail")
                    overall = ReportGenerator._overall_status(results)
                    if overall in ("fail", "error"):
                        overall_exit_code = 1

                    pair_summaries.append({
                        "name": pair_name,
                        "overall_status": overall,
                        "table_count": len(results),
                        "pass_count": pass_count,
                        "fail_count": fail_count,
                        "elapsed_seconds": 0,
                        "report_file": Path(report_file).name,
                    })

            # Summary dashboard
            if pair_summaries:
                gen = ReportGenerator(report_cfg.get("output_dir", "output/reports"))
                summary_file = gen.generate_summary(pair_summaries)
                self.log_signal.emit(f"Summary dashboard: {summary_file}")

            self.progress_signal.emit(100, "Complete")
            root_logger.removeHandler(handler)

            self.finished_signal.emit(
                overall_exit_code == 0,
                str(Path(last_report_dir).resolve()),
            )

        except Exception as e:
            self.log_signal.emit(f"ERROR: {e}")
            self.log_signal.emit(traceback.format_exc())
            self.finished_signal.emit(False, str(e))

    def _export_pair(self, client, pair, export_cfg, side):
        from validator import export_report_data
        export_report_data(
            client=client,
            dxp_path=pair[f"{side}_dxp"],
            output_dir=export_cfg.get("output_dir", "output/exports"),
            bookmark=pair.get("bookmark", ""),
            data_tables=pair.get("data_tables", []),
            max_rows=export_cfg.get("max_rows", 10000),
            poll_interval=export_cfg.get("poll_interval", 10),
            job_timeout=export_cfg.get("job_timeout", 1800),
            pair_name=pair["name"],
            side=side,
        )

    def _compare(self, as_is_dir, to_be_dir, comp_cfg, table_names):
        from validator import compare_exported_data
        return compare_exported_data(as_is_dir, to_be_dir, comp_cfg, table_names)

    def _generate_report(self, pair, results, report_cfg):
        from validator import generate_reports
        return generate_reports(
            pair_name=pair["name"],
            as_is_dxp=pair["as_is_dxp"],
            to_be_dxp=pair["to_be_dxp"],
            bookmark=pair.get("bookmark", ""),
            results=results,
            report_dir=report_cfg.get("output_dir", "output/reports"),
        )


class GuiLogHandler(logging.Handler):
    """Redirects log records to the GUI log panel via signal."""

    def __init__(self, log_signal):
        super().__init__()
        self._log_signal = log_signal

    def emit(self, record):
        msg = self.format(record)
        self._log_signal.emit(msg)


# ═══════════════════════════════════════════════════════════════
#  Main Window
# ═══════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Spotfire Report Validator")
        self.setMinimumSize(720, 760)
        self.resize(760, 820)

        self._worker: ValidationWorker | None = None
        self._config_path = BASE_DIR / "config_gui.yaml"

        self._build_ui()
        self._load_existing_config()

    # ── UI Construction ────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        # Header
        header = QLabel("Spotfire Report Validator")
        header.setStyleSheet("font-size: 22px; font-weight: 700; color: #1a73e8; padding: 0;")
        root.addWidget(header)

        subheader = QLabel("Teradata → BigQuery migration validation")
        subheader.setStyleSheet("font-size: 13px; color: #5f6368; padding: 0 0 4px 0;")
        root.addWidget(subheader)

        # Scrollable form area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")

        form_widget = QWidget()
        form_layout = QVBoxLayout(form_widget)
        form_layout.setSpacing(10)

        # ── Spotfire Connection ──
        conn_group = QGroupBox("Spotfire Server Connection")
        conn_form = QFormLayout(conn_group)
        conn_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        conn_form.setSpacing(8)

        self.server_url = QLineEdit(placeholderText="https://spotfire.company.com/spotfire")
        self.api_base = QLineEdit("/api/rest")
        self.auth_type = QComboBox()
        self.auth_type.addItems(["basic", "oauth"])
        self.username = QLineEdit(placeholderText="automation_user")
        self.password = QLineEdit(placeholderText="Password or set SPOTFIRE_PASSWORD env")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 3600)
        self.timeout.setValue(120)
        self.timeout.setSuffix(" s")

        conn_form.addRow("Server URL", self.server_url)
        conn_form.addRow("API Base", self.api_base)
        conn_form.addRow("Auth Type", self.auth_type)
        conn_form.addRow("Username", self.username)
        conn_form.addRow("Password", self.password)
        conn_form.addRow("Timeout", self.timeout)

        form_layout.addWidget(conn_group)

        # ── Export Settings ──
        export_group = QGroupBox("Export Settings")
        export_form = QFormLayout(export_group)
        export_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        export_form.setSpacing(8)

        self.export_dir = QLineEdit("output/exports")
        export_browse = QPushButton("Browse…", objectName="secondary")
        export_browse.clicked.connect(lambda: self._browse_dir(self.export_dir))
        export_dir_row = QHBoxLayout()
        export_dir_row.addWidget(self.export_dir, 1)
        export_dir_row.addWidget(export_browse)
        export_form.addRow("Output Dir", self._wrap_h(export_dir_row))

        self.max_rows = QSpinBox()
        self.max_rows.setRange(0, 100000000)
        self.max_rows.setValue(10000)
        export_form.addRow("Max Rows", self.max_rows)

        self.poll_interval = QSpinBox()
        self.poll_interval.setRange(1, 600)
        self.poll_interval.setValue(10)
        self.poll_interval.setSuffix(" s")
        export_form.addRow("Poll Interval", self.poll_interval)

        self.job_timeout = QSpinBox()
        self.job_timeout.setRange(10, 7200)
        self.job_timeout.setValue(1800)
        self.job_timeout.setSuffix(" s")
        export_form.addRow("Job Timeout", self.job_timeout)

        form_layout.addWidget(export_group)

        # ── Comparison Settings ──
        comp_group = QGroupBox("Comparison Settings")
        comp_form = QFormLayout(comp_group)
        comp_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        comp_form.setSpacing(8)

        self.float_precision = QSpinBox()
        self.float_precision.setRange(0, 15)
        self.float_precision.setValue(6)
        comp_form.addRow("Float Precision", self.float_precision)

        self.row_tolerance = QLineEdit("0.0")
        comp_form.addRow("Row Count Tolerance %", self.row_tolerance)

        self.timestamp_format = QLineEdit("%Y-%m-%d %H:%M:%S")
        comp_form.addRow("Timestamp Format", self.timestamp_format)

        self.null_markers = QLineEdit('"", NULL, None, NaN, \\N, null')
        comp_form.addRow("Null Markers", self.null_markers)

        form_layout.addWidget(comp_group)

        # ── Report Output ──
        report_group = QGroupBox("Report Output")
        report_form = QFormLayout(report_group)
        report_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        report_form.setSpacing(8)

        self.report_dir = QLineEdit("output/reports")
        report_browse = QPushButton("Browse…", objectName="secondary")
        report_browse.clicked.connect(lambda: self._browse_dir(self.report_dir))
        report_row = QHBoxLayout()
        report_row.addWidget(self.report_dir, 1)
        report_row.addWidget(report_browse)
        report_form.addRow("Report Dir", self._wrap_h(report_row))

        form_layout.addWidget(report_group)

        # ── Report Pairs ──
        pairs_group = QGroupBox("Report Pairs")
        pairs_layout = QVBoxLayout(pairs_group)

        pairs_label = QLabel("Define one or more report pairs to validate. Use the + / − buttons to add or remove pairs.")
        pairs_label.setStyleSheet("color: #5f6368; font-size: 12px;")
        pairs_layout.addWidget(pairs_label)

        self.pairs_container = QVBoxLayout()
        pairs_layout.addLayout(self.pairs_container)

        self.pair_widgets: list[dict[str, Any]] = []

        # Add / remove buttons
        pair_btn_row = QHBoxLayout()
        add_pair_btn = QPushButton("+  Add Pair", objectName="secondary")
        add_pair_btn.clicked.connect(self._add_pair)
        pair_btn_row.addWidget(add_pair_btn)

        self.remove_pair_btn = QPushButton("−  Remove Last", objectName="secondary")
        self.remove_pair_btn.clicked.connect(self._remove_last_pair)
        pair_btn_row.addWidget(self.remove_pair_btn)
        pair_btn_row.addStretch()
        pairs_layout.addLayout(pair_btn_row)

        form_layout.addWidget(pairs_group)

        scroll.setWidget(form_widget)
        root.addWidget(scroll, 1)

        # ── Progress + Log ──
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Idle")
        root.addWidget(self.progress_bar)

        self.log_panel = QPlainTextEdit()
        self.log_panel.setReadOnly(True)
        self.log_panel.setMaximumHeight(160)
        self.log_panel.setPlaceholderText("Log output will appear here…")
        root.addWidget(self.log_panel)

        # ── Action Buttons ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self.clear_btn = QPushButton("Clear", objectName="secondary")
        self.clear_btn.clicked.connect(self._clear_log)
        btn_row.addWidget(self.clear_btn)

        self.run_btn = QPushButton("▶  Run Validation", objectName="primary")
        self.run_btn.clicked.connect(self._run_validation)
        btn_row.addWidget(self.run_btn)

        root.addLayout(btn_row)

        # Start with one pair
        self._add_pair()

    def _wrap_h(self, h_layout: QHBoxLayout) -> QWidget:
        """Wrap a QHBoxLayout in a QWidget so it can be used in QFormLayout."""
        w = QWidget()
        w.setLayout(h_layout)
        h_layout.setContentsMargins(0, 0, 0, 0)
        return w

    # ── Report Pair Widgets ────────────────────────────────────

    def _add_pair(self, data: dict[str, Any] | None = None):
        """Add a report pair input section."""
        data = data or {}
        idx = len(self.pair_widgets)

        pair_box = QGroupBox(f"Pair {idx + 1}")
        form = QFormLayout(pair_box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(6)

        name = QLineEdit(data.get("name", ""))
        name.setPlaceholderText("e.g. Cargo Revenue Report")
        form.addRow("Name", name)

        as_is = QLineEdit(data.get("as_is_dxp", ""))
        as_is.setPlaceholderText("/Users/Cargo/Revenue_Report_TD")
        form.addRow("As-Is DXP (Teradata)", as_is)

        to_be = QLineEdit(data.get("to_be_dxp", ""))
        to_be.setPlaceholderText("/Users/Cargo/Revenue_Report_BQ")
        form.addRow("To-Be DXP (BigQuery)", to_be)

        bookmark = QLineEdit(data.get("bookmark", ""))
        bookmark.setPlaceholderText("(optional) e.g. Q1_2026_Validation")
        form.addRow("Bookmark", bookmark)

        data_tables = QLineEdit(", ".join(data.get("data_tables", [])) if data.get("data_tables") else "")
        data_tables.setPlaceholderText("(optional) comma-separated, empty = all tables")
        form.addRow("Data Tables", data_tables)

        self.pairs_container.addWidget(pair_box)
        self.pair_widgets.append({
            "group": pair_box,
            "name": name,
            "as_is": as_is,
            "to_be": to_be,
            "bookmark": bookmark,
            "data_tables": data_tables,
        })

        self.remove_pair_btn.setEnabled(len(self.pair_widgets) > 1)

    def _remove_last_pair(self):
        if len(self.pair_widgets) <= 1:
            return
        pw = self.pair_widgets.pop()
        pw["group"].setParent(None)
        pw["group"].deleteLater()
        self.remove_pair_btn.setEnabled(len(self.pair_widgets) > 1)

    # ── Helpers ────────────────────────────────────────────────

    def _browse_dir(self, line_edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Select Directory")
        if d:
            line_edit.setText(d)

    def _clear_log(self):
        self.log_panel.clear()

    def _load_existing_config(self):
        """Try to load existing config.yaml to pre-fill fields."""
        cfg_path = BASE_DIR / "config.yaml"
        if not cfg_path.exists():
            return
        try:
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f)
            if not cfg:
                return

            sf = cfg.get("spotfire", {})
            self.server_url.setText(sf.get("server_url", ""))
            self.api_base.setText(sf.get("api_base", "/api/rest"))
            self.auth_type.setCurrentText(sf.get("auth_type", "basic"))
            self.username.setText(sf.get("username", ""))
            self.timeout.setValue(sf.get("timeout", 120))

            exp = cfg.get("export", {})
            self.export_dir.setText(exp.get("output_dir", "output/exports"))
            self.max_rows.setValue(exp.get("max_rows", 10000))
            self.poll_interval.setValue(exp.get("poll_interval", 10))
            self.job_timeout.setValue(exp.get("job_timeout", 1800))

            comp = cfg.get("comparison", {})
            self.float_precision.setValue(comp.get("float_precision", 6))
            self.row_tolerance.setText(str(comp.get("row_count_tolerance_pct", 0.0)))
            self.timestamp_format.setText(comp.get("timestamp_format", "%Y-%m-%d %H:%M:%S"))
            null_markers = comp.get("null_markers", [])
            if isinstance(null_markers, list):
                self.null_markers.setText(", ".join(str(m) for m in null_markers))

            rep = cfg.get("report", {})
            self.report_dir.setText(rep.get("output_dir", "output/reports"))

            pairs = cfg.get("report_pairs", [])
            if pairs:
                # Clear default empty pair
                while self.pair_widgets:
                    self._remove_last_pair()
                for p in pairs:
                    self._add_pair(p)

        except Exception as e:
            logger.debug("Could not load existing config: %s", e)

    def _collect_config(self) -> dict[str, Any]:
        """Collect all form fields into a config dict."""
        null_markers_raw = self.null_markers.text().strip()
        null_markers = [m.strip() for m in null_markers_raw.split(",")] if null_markers_raw else []

        pairs = []
        for pw in self.pair_widgets:
            dt_raw = pw["data_tables"].text().strip()
            data_tables = [t.strip() for t in dt_raw.split(",")] if dt_raw else []
            pairs.append({
                "name": pw["name"].text().strip(),
                "as_is_dxp": pw["as_is"].text().strip(),
                "to_be_dxp": pw["to_be"].text().strip(),
                "bookmark": pw["bookmark"].text().strip(),
                "data_tables": data_tables,
            })

        return {
            "spotfire": {
                "server_url": self.server_url.text().strip(),
                "api_base": self.api_base.text().strip(),
                "auth_type": self.auth_type.currentText(),
                "username": self.username.text().strip(),
                "password": self.password.text() or "",
                "timeout": self.timeout.value(),
            },
            "export": {
                "output_dir": self.export_dir.text().strip(),
                "max_rows": self.max_rows.value(),
                "format": "csv",
                "poll_interval": self.poll_interval.value(),
                "job_timeout": self.job_timeout.value(),
            },
            "comparison": {
                "sort_columns": [],
                "float_precision": self.float_precision.value(),
                "null_markers": null_markers,
                "timestamp_format": self.timestamp_format.text().strip(),
                "row_count_tolerance_pct": float(self.row_tolerance.text().strip() or "0.0"),
                "max_diff_rows_display": 50,
            },
            "report": {
                "output_dir": self.report_dir.text().strip(),
            },
            "report_pairs": pairs,
        }

    def _validate_form(self) -> str | None:
        """Validate required fields. Returns error message or None."""
        if not self.server_url.text().strip():
            return "Server URL is required."
        if not self.username.text().strip():
            return "Username is required."
        for i, pw in enumerate(self.pair_widgets):
            if not pw["name"].text().strip():
                return f"Pair {i+1}: Name is required."
            if not pw["as_is"].text().strip():
                return f"Pair {i+1}: As-Is DXP path is required."
            if not pw["to_be"].text().strip():
                return f"Pair {i+1}: To-Be DXP path is required."
        return None

    # ── Actions ────────────────────────────────────────────────

    def _run_validation(self):
        error = self._validate_form()
        if error:
            QMessageBox.warning(self, "Validation Error", error)
            return

        config_data = self._collect_config()
        self._config_path = BASE_DIR / "config_gui.yaml"

        self.run_btn.setEnabled(False)
        self.run_btn.setText("Running…")
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Starting…")
        self.log_panel.clear()

        self._worker = ValidationWorker(config_data, self._config_path)
        self._worker.log_signal.connect(self._on_log)
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.finished_signal.connect(self._on_finished)
        self._worker.start()

    def _on_log(self, msg: str):
        self.log_panel.appendPlainText(msg)

    def _on_progress(self, pct: int, status: str):
        self.progress_bar.setValue(pct)
        self.progress_bar.setFormat(f"{status}  ({pct}%)")

    def _on_finished(self, success: bool, report_path: str):
        self.run_btn.setEnabled(True)
        self.run_btn.setText("▶  Run Validation")

        if success:
            self.progress_bar.setFormat("Complete ✓")
            self.progress_bar.setValue(100)
            self.log_panel.appendPlainText("")
            self.log_panel.appendPlainText(f"✅ Reports generated at: {report_path}")

            msg_box = QMessageBox(self)
            msg_box.setIcon(QMessageBox.Icon.Information)
            msg_box.setWindowTitle("Validation Complete")
            msg_box.setText("Validation completed successfully.")
            msg_box.setInformativeText(f"Reports generated at:\n{report_path}")

            open_btn = msg_box.addButton("Open Report Folder", QMessageBox.ButtonRole.AcceptRole)
            msg_box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
            msg_box.exec()

            if msg_box.clickedButton() == open_btn:
                self._open_in_file_manager(report_path)
        else:
            self.progress_bar.setFormat("Failed")
            self.log_panel.appendPlainText(f"❌ Error: {report_path}")
            QMessageBox.critical(self, "Validation Failed", report_path)

    def _open_in_file_manager(self, path: str):
        """Open the file manager at the given path (cross-platform)."""
        import subprocess
        p = Path(path)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Spotfire Report Validator")
    app.setStyleSheet(STYLESHEET)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()