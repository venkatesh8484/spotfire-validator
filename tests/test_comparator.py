"""
Tests for the Spotfire Validator comparison engine.

Run with:  python -m pytest tests/  -v
Or:        python tests/test_comparator.py
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from comparator import ComparisonConfig, DataComparator, TableComparisonResult


# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

def write_csv(path: str, columns: list[str], rows: list[list[str]]) -> str:
    """Write a CSV file and return its path."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(row)
    return path


def make_config(**overrides) -> ComparisonConfig:
    """Create a ComparisonConfig with defaults + overrides."""
    defaults = {
        "sort_columns": [],
        "float_precision": 6,
        "null_markers": ["", "NULL", "None", "NaN", "\\N", "null"],
        "timestamp_format": "%Y-%m-%d %H:%M:%S",
        "row_count_tolerance_pct": 0.0,
        "max_diff_rows_display": 50,
    }
    defaults.update(overrides)
    return ComparisonConfig.from_dict(defaults)


# ═══════════════════════════════════════════════════════════════
#  Test cases
# ═══════════════════════════════════════════════════════════════

class TestMatchingData:
    """Identical CSVs should pass."""

    def test_identical_csvs_pass(self, tmp_path):
        cols = ["id", "name", "amount"]
        rows = [
            ["1", "Alice", "100.50"],
            ["2", "Bob", "200.00"],
            ["3", "Charlie", "300.25"],
        ]
        csv1 = write_csv(str(tmp_path / "as_is" / "data.csv"), cols, rows)
        csv2 = write_csv(str(tmp_path / "to_be" / "data.csv"), cols, rows)

        comparator = DataComparator(make_config())
        result = comparator.compare_csvs(csv1, csv2, "data")

        assert result.status == "pass"
        assert result.row_count_match is True
        assert result.columns_match is True
        assert result.checksum_match is True
        assert len(result.column_diffs) == 3
        assert all(cd.value_match for cd in result.column_diffs)

    def test_identical_single_row(self, tmp_path):
        cols = ["x"]
        rows = [["42"]]
        csv1 = write_csv(str(tmp_path / "a" / "d.csv"), cols, rows)
        csv2 = write_csv(str(tmp_path / "b" / "d.csv"), cols, rows)

        comparator = DataComparator(make_config())
        result = comparator.compare_csvs(csv1, csv2, "d")

        assert result.status == "pass"


class TestRowOrderDifference:
    """Same data in different row order should pass (sorted before compare)."""

    def test_different_row_order_passes(self, tmp_path):
        cols = ["id", "name"]
        # as-is is NOT sorted by id; to-be IS sorted by id
        rows1 = [["3", "Charlie"], ["1", "Alice"], ["2", "Bob"]]
        rows2 = [["1", "Alice"], ["2", "Bob"], ["3", "Charlie"]]

        csv1 = write_csv(str(tmp_path / "a" / "data.csv"), cols, rows1)
        csv2 = write_csv(str(tmp_path / "b" / "data.csv"), cols, rows2)

        comparator = DataComparator(make_config())
        result = comparator.compare_csvs(csv1, csv2, "data")

        assert result.status == "pass"
        assert result.row_order_differs is True
        assert result.checksum_match is True


class TestValueMismatch:
    """Different values should fail."""

    def test_value_mismatch_fails(self, tmp_path):
        cols = ["id", "amount"]
        rows1 = [["1", "100.50"], ["2", "200.00"]]
        rows2 = [["1", "100.50"], ["2", "999.99"]]

        csv1 = write_csv(str(tmp_path / "a" / "data.csv"), cols, rows1)
        csv2 = write_csv(str(tmp_path / "b" / "data.csv"), cols, rows2)

        comparator = DataComparator(make_config())
        result = comparator.compare_csvs(csv1, csv2, "data")

        assert result.status == "fail"
        assert result.row_count_match is True
        # The 'amount' column should have mismatches
        amount_diff = next(
            cd for cd in result.column_diffs if cd.column_name == "amount"
        )
        assert amount_diff.mismatch_count == 1
        assert len(amount_diff.sample_mismatches) == 1
        assert amount_diff.sample_mismatches[0]["as_is"] == "200.000000"
        assert amount_diff.sample_mismatches[0]["to_be"] == "999.990000"


class TestRowCountMismatch:
    """Different row counts should fail."""

    def test_more_rows_in_as_is(self, tmp_path):
        cols = ["id"]
        rows1 = [["1"], ["2"], ["3"]]
        rows2 = [["1"], ["2"]]

        csv1 = write_csv(str(tmp_path / "a" / "data.csv"), cols, rows1)
        csv2 = write_csv(str(tmp_path / "b" / "data.csv"), cols, rows2)

        comparator = DataComparator(make_config())
        result = comparator.compare_csvs(csv1, csv2, "data")

        assert result.status == "fail"
        assert result.row_count_as_is == 3
        assert result.row_count_to_be == 2
        assert result.row_count_match is False

    def test_row_count_tolerance(self, tmp_path):
        """With tolerance, small row count diff should pass."""
        cols = ["id"]
        rows1 = [["1"], ["2"], ["3"], ["4"], ["5"], ["6"], ["7"], ["8"], ["9"], ["10"]]
        rows2 = [["1"], ["2"], ["3"], ["4"], ["5"], ["6"], ["7"], ["8"], ["9"]]

        csv1 = write_csv(str(tmp_path / "a" / "data.csv"), cols, rows1)
        csv2 = write_csv(str(tmp_path / "b" / "data.csv"), cols, rows2)

        # 1/10 = 10% diff, tolerance 15% → pass
        comparator = DataComparator(make_config(row_count_tolerance_pct=15.0))
        result = comparator.compare_csvs(csv1, csv2, "data")

        assert result.row_count_match is True
        # But rows_only_in_as_is > 0, so still fail
        assert result.status == "fail"


class TestColumnMismatch:
    """Different columns should fail."""

    def test_extra_column_in_as_is(self, tmp_path):
        csv1 = write_csv(
            str(tmp_path / "a" / "data.csv"),
            ["id", "name", "extra_col"],
            [["1", "Alice", "X"]],
        )
        csv2 = write_csv(
            str(tmp_path / "b" / "data.csv"),
            ["id", "name"],
            [["1", "Alice"]],
        )

        comparator = DataComparator(make_config())
        result = comparator.compare_csvs(csv1, csv2, "data")

        assert result.status == "fail"
        assert "extra_col" in result.columns_only_in_as_is
        assert result.columns_match is False

    def test_different_column_names(self, tmp_path):
        csv1 = write_csv(
            str(tmp_path / "a" / "data.csv"),
            ["id", "customer_name"],
            [["1", "Alice"]],
        )
        csv2 = write_csv(
            str(tmp_path / "b" / "data.csv"),
            ["id", "customer_name"],
            [["1", "Alice"]],
        )

        comparator = DataComparator(make_config())
        result = comparator.compare_csvs(csv1, csv2, "data")

        assert result.status == "pass"
        assert result.columns_match is True


class TestTypeNormalization:
    """Type normalization: timestamps, nulls, float precision."""

    def test_timestamp_normalization(self, tmp_path):
        """Same timestamp in different formats should match."""
        csv1 = write_csv(
            str(tmp_path / "a" / "data.csv"),
            ["id", "ts"],
            [["1", "2026-01-15 10:30:00"], ["2", "2026-02-20 14:00:00"]],
        )
        csv2 = write_csv(
            str(tmp_path / "b" / "data.csv"),
            ["id", "ts"],
            [["1", "2026-01-15T10:30:00"], ["2", "2026-02-20T14:00:00"]],
        )

        comparator = DataComparator(make_config())
        result = comparator.compare_csvs(csv1, csv2, "data")

        assert result.status == "pass"

    def test_null_normalization(self, tmp_path):
        """Different null representations should match."""
        csv1 = write_csv(
            str(tmp_path / "a" / "data.csv"),
            ["id", "val"],
            [["1", "NULL"], ["2", "100"], ["3", ""], ["4", "NaN"]],
        )
        csv2 = write_csv(
            str(tmp_path / "b" / "data.csv"),
            ["id", "val"],
            [["1", ""], ["2", "100"], ["3", "NULL"], ["4", "None"]],
        )

        comparator = DataComparator(make_config())
        result = comparator.compare_csvs(csv1, csv2, "data")

        assert result.status == "pass"

    def test_float_precision(self, tmp_path):
        """Floats with different precision should match after normalization."""
        csv1 = write_csv(
            str(tmp_path / "a" / "data.csv"),
            ["id", "amount"],
            [["1", "100.5000000"], ["2", "200.123456789"]],
        )
        csv2 = write_csv(
            str(tmp_path / "b" / "data.csv"),
            ["id", "amount"],
            [["1", "100.5"], ["2", "200.123457"]],
        )

        comparator = DataComparator(make_config(float_precision=6))
        result = comparator.compare_csvs(csv1, csv2, "data")

        assert result.status == "pass"


class TestEmptyData:
    """Edge cases with empty data."""

    def test_both_empty_pass(self, tmp_path):
        csv1 = write_csv(
            str(tmp_path / "a" / "data.csv"), ["id", "name"], []
        )
        csv2 = write_csv(
            str(tmp_path / "b" / "data.csv"), ["id", "name"], []
        )

        comparator = DataComparator(make_config())
        result = comparator.compare_csvs(csv1, csv2, "data")

        assert result.status == "pass"
        assert result.row_count_as_is == 0
        assert result.row_count_to_be == 0

    def test_one_empty_fails(self, tmp_path):
        csv1 = write_csv(
            str(tmp_path / "a" / "data.csv"), ["id"], [["1"], ["2"]]
        )
        csv2 = write_csv(
            str(tmp_path / "b" / "data.csv"), ["id"], []
        )

        comparator = DataComparator(make_config())
        result = comparator.compare_csvs(csv1, csv2, "data")

        assert result.status == "fail"
        assert result.row_count_as_is == 2
        assert result.row_count_to_be == 0


class TestExportJobBuilder:
    """Test the Automation Services job XML builder."""

    def test_build_job_with_bookmark(self):
        from export_job_builder import build_export_job

        xml = build_export_job(
            analysis_path="/Users/Cargo/Revenue_Report_TD",
            output_dir="/tmp/spotfire_validator",
            output_file="data.csv",
            data_table_name="SalesData",
            bookmark="Q1_2026_Validation",
            max_rows=10000,
        )

        assert "Revenue_Report_TD" in xml
        assert "Q1_2026_Validation" in xml
        assert "SalesData" in xml
        assert "data.csv" in xml
        assert "SetBookmark" in xml
        assert "10000" in xml

    def test_build_job_without_bookmark(self):
        from export_job_builder import build_export_job

        xml = build_export_job(
            analysis_path="/Users/Cargo/Revenue_Report_BQ",
            output_dir="/tmp/spotfire_validator",
            output_file="data.csv",
            data_table_name="*",
            bookmark="",
            max_rows=0,
        )

        assert "SetBookmark" not in xml
        assert "Revenue_Report_BQ" in xml

    def test_xml_escaping(self):
        from export_job_builder import build_export_job

        xml = build_export_job(
            analysis_path="/Users/Test & <Special>/Report",
            output_dir="/tmp",
            output_file="out.csv",
            data_table_name="Table",
            bookmark='Bookmark "Test"',
            max_rows=100,
        )

        assert "&amp;" in xml
        assert "&lt;" in xml
        assert "&gt;" in xml


# ═══════════════════════════════════════════════════════════════
#  Main entry for running without pytest
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))