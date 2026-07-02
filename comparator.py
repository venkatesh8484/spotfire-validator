"""
Comparator — pandas-based data table comparison engine.

Compares two exported CSVs (as-is Teradata report vs. to-be BigQuery report)
with type normalization, sorting, and multi-level checks.

Reuses the checksum pattern from ``bo-bq-migrator/src/validator.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  Result data classes
# ═══════════════════════════════════════════════════════════════

@dataclass
class ColumnDiff:
    """Per-column comparison result."""
    column_name: str
    type_match: bool = False
    value_match: bool = False
    mismatch_count: int = 0
    sample_mismatches: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TableComparisonResult:
    """Full comparison result for one data table pair."""
    table_name: str
    status: str = "pending"  # pass | fail | warn | error | pending

    # Row counts
    row_count_as_is: int = 0
    row_count_to_be: int = 0
    row_count_match: bool = False
    row_count_diff_pct: float = 0.0

    # Column checks
    columns_as_is: list[str] = field(default_factory=list)
    columns_to_be: list[str] = field(default_factory=list)
    columns_match: bool = False
    columns_only_in_as_is: list[str] = field(default_factory=list)
    columns_only_in_to_be: list[str] = field(default_factory=list)

    # Checksums
    checksum_as_is: str = ""
    checksum_to_be: str = ""
    checksum_match: bool = False

    # Per-column diffs
    column_diffs: list[ColumnDiff] = field(default_factory=list)

    # Row-level diffs
    rows_only_in_as_is: int = 0
    rows_only_in_to_be: int = 0
    row_order_differs: bool = False
    sample_diff_rows: list[dict[str, Any]] = field(default_factory=list)

    # Metadata
    error: str = ""
    elapsed_seconds: float = 0.0


@dataclass
class ComparisonConfig:
    """Configuration for the comparison engine."""
    sort_columns: list[str] = field(default_factory=list)
    float_precision: int = 6
    null_markers: list[str] = field(
        default_factory=lambda: ["", "NULL", "None", "NaN", "\\N", "null"]
    )
    timestamp_format: str = "%Y-%m-%d %H:%M:%S"
    row_count_tolerance_pct: float = 0.0
    max_diff_rows_display: int = 50

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComparisonConfig":
        return cls(
            sort_columns=data.get("sort_columns", []),
            float_precision=data.get("float_precision", 6),
            null_markers=data.get(
                "null_markers", ["", "NULL", "None", "NaN", "\\N", "null"]
            ),
            timestamp_format=data.get(
                "timestamp_format", "%Y-%m-%d %H:%M:%S"
            ),
            row_count_tolerance_pct=data.get(
                "row_count_tolerance_pct", 0.0
            ),
            max_diff_rows_display=data.get("max_diff_rows_display", 50),
        )


# ═══════════════════════════════════════════════════════════════
#  Comparator
# ═══════════════════════════════════════════════════════════════

class DataComparator:
    """
    Compares two pandas DataFrames representing Spotfire data tables.

    Handles:
      - Type normalization (timestamps, nulls, float precision)
      - Sort-before-compare (non-deterministic row ordering)
      - Row count comparison (with tolerance)
      - Column name comparison
      - Value-level comparison with sample mismatch capture
      - Checksum comparison
    """

    def __init__(self, config: ComparisonConfig):
        self.config = config

    def compare_csvs(
        self,
        as_is_csv: str,
        to_be_csv: str,
        table_name: str = "unknown",
    ) -> TableComparisonResult:
        """
        Load two CSVs and compare them.

        Parameters
        ----------
        as_is_csv, to_be_csv
            File paths to the exported CSVs.
        table_name
            Name of the data table (for reporting).

        Returns
        -------
        TableComparisonResult
        """
        import time

        start = time.time()
        result = TableComparisonResult(table_name=table_name)

        try:
            df_as_is = pd.read_csv(as_is_csv, dtype=str, keep_default_na=False)
            df_to_be = pd.read_csv(to_be_csv, dtype=str, keep_default_na=False)
        except Exception as e:
            result.status = "error"
            result.error = f"Failed to load CSVs: {e}"
            result.elapsed_seconds = time.time() - start
            return result

        return self.compare_dataframes(
            df_as_is, df_to_be, table_name, start
        )

    def compare_dataframes(
        self,
        df_as_is: pd.DataFrame,
        df_to_be: pd.DataFrame,
        table_name: str = "unknown",
        start_time: float | None = None,
    ) -> TableComparisonResult:
        """
        Compare two in-memory DataFrames.

        Parameters
        ----------
        df_as_is, df_to_be
            DataFrames to compare (all columns as strings for normalization).
        table_name
            Name of the data table (for reporting).
        start_time
            Optional start timestamp for elapsed time.

        Returns
        -------
        TableComparisonResult
        """
        import time

        if start_time is None:
            start_time = time.time()

        result = TableComparisonResult(table_name=table_name)

        # ── 1. Normalize ──────────────────────────────────────
        df_as_is = self._normalize(df_as_is)
        df_to_be = self._normalize(df_to_be)

        # ── 2. Row counts ─────────────────────────────────────
        result.row_count_as_is = len(df_as_is)
        result.row_count_to_be = len(df_to_be)

        if result.row_count_as_is == 0 and result.row_count_to_be == 0:
            result.row_count_match = True
            result.status = "pass"
            result.elapsed_seconds = time.time() - start_time
            return result

        if result.row_count_as_is > 0:
            diff = abs(result.row_count_as_is - result.row_count_to_be)
            pct = (diff / result.row_count_as_is) * 100
            result.row_count_diff_pct = round(pct, 4)
            result.row_count_match = pct <= self.config.row_count_tolerance_pct
        else:
            result.row_count_match = False
            result.row_count_diff_pct = 100.0

        # ── 3. Column comparison ──────────────────────────────
        result.columns_as_is = list(df_as_is.columns)
        result.columns_to_be = list(df_to_be.columns)
        result.columns_only_in_as_is = list(
            set(df_as_is.columns) - set(df_to_be.columns)
        )
        result.columns_only_in_to_be = list(
            set(df_to_be.columns) - set(df_as_is.columns)
        )
        common_cols = sorted(set(df_as_is.columns) & set(df_to_be.columns))
        result.columns_match = (
            not result.columns_only_in_as_is
            and not result.columns_only_in_to_be
            and list(df_as_is.columns) == list(df_to_be.columns)
        )

        if not common_cols:
            result.status = "fail"
            result.error = "No common columns between as-is and to-be"
            result.elapsed_seconds = time.time() - start_time
            return result

        # Align to common columns
        df_as_is = df_as_is[common_cols].copy()
        df_to_be = df_to_be[common_cols].copy()

        # If columns don't match exactly, it's a fail
        if not result.columns_match:
            result.status = "fail"
            result.elapsed_seconds = time.time() - start_time
            return result

        # ── 4. Sort before compare ────────────────────────────
        sort_cols = self.config.sort_columns or common_cols
        sort_cols = [c for c in sort_cols if c in common_cols]

        df_as_is_sorted = df_as_is.sort_values(
            by=sort_cols, kind="mergesort"
        ).reset_index(drop=True)
        df_to_be_sorted = df_to_be.sort_values(
            by=sort_cols, kind="mergesort"
        ).reset_index(drop=True)

        # Check if original row order differed
        result.row_order_differs = not df_as_is.equals(df_as_is_sorted)

        # ── 5. Checksums ─────────────────────────────────────
        result.checksum_as_is = self._compute_checksum(df_as_is_sorted)
        result.checksum_to_be = self._compute_checksum(df_to_be_sorted)
        result.checksum_match = (
            result.checksum_as_is == result.checksum_to_be
        )

        # ── 6. Per-column value comparison ───────────────────
        all_values_match = True
        for col in common_cols:
            col_diff = ColumnDiff(column_name=col)
            col_diff.type_match = True  # all strings after normalization

            s1 = df_as_is_sorted[col]
            s2 = df_to_be_sorted[col]

            # Align lengths for comparison
            min_len = min(len(s1), len(s2))
            s1_aligned = s1.iloc[:min_len].reset_index(drop=True)
            s2_aligned = s2.iloc[:min_len].reset_index(drop=True)

            mismatches = s1_aligned != s2_aligned
            mismatch_count = int(mismatches.sum())
            col_diff.mismatch_count = mismatch_count
            col_diff.value_match = mismatch_count == 0

            if mismatch_count > 0:
                all_values_match = False
                # Capture sample mismatches
                mismatch_indices = mismatches[mismatches].index[
                    : min(5, mismatch_count)
                ]
                for idx in mismatch_indices:
                    col_diff.sample_mismatches.append(
                        {
                            "row": int(idx),
                            "as_is": str(s1_aligned.iloc[idx]),
                            "to_be": str(s2_aligned.iloc[idx]),
                        }
                    )

            result.column_diffs.append(col_diff)

        # ── 7. Row-level set difference ───────────────────────
        if result.row_count_match:
            # Use merged indicator to find rows only in one side
            try:
                merged = df_as_is_sorted.merge(
                    df_to_be_sorted,
                    how="outer",
                    indicator=True,
                    on=common_cols,
                )
                result.rows_only_in_as_is = int(
                    (merged["_merge"] == "left_only").sum()
                )
                result.rows_only_in_to_be = int(
                    (merged["_merge"] == "right_only").sum()
                )
            except Exception:
                # Fallback if merge fails (e.g. unhashable types)
                result.rows_only_in_as_is = max(
                    0, result.row_count_as_is - result.row_count_to_be
                )
                result.rows_only_in_to_be = max(
                    0, result.row_count_to_be - result.row_count_as_is
                )
        else:
            result.rows_only_in_as_is = max(
                0, result.row_count_as_is - result.row_count_to_be
            )
            result.rows_only_in_to_be = max(
                0, result.row_count_to_be - result.row_count_as_is
            )

        # ── 8. Sample diff rows ───────────────────────────────
        if not all_values_match or not result.row_count_match:
            result.sample_diff_rows = self._collect_sample_diffs(
                df_as_is_sorted, df_to_be_sorted, common_cols
            )

        # ── 9. Overall verdict ─────────────────────────────────
        if not result.row_count_match:
            result.status = "fail"
        elif result.rows_only_in_as_is > 0 or result.rows_only_in_to_be > 0:
            result.status = "fail"
        elif not all_values_match:
            result.status = "fail"
        elif result.row_order_differs and not result.checksum_match:
            result.status = "warn"
        else:
            result.status = "pass"

        result.elapsed_seconds = time.time() - start_time
        return result

    # ──────────────────────────────────────────────
    #  Normalization
    # ──────────────────────────────────────────────

    def _normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normalize a DataFrame: nulls, timestamps, float precision."""
        null_set = set(self.config.null_markers)

        for col in df.columns:
            # Normalize nulls
            df[col] = df[col].apply(
                lambda v: "<NULL>" if str(v).strip() in null_set else str(v).strip()
            )

            # Try timestamp normalization
            df[col] = df[col].apply(lambda v: self._normalize_timestamp(v))

            # Try float normalization
            df[col] = df[col].apply(lambda v: self._normalize_float(v))

        return df

    def _normalize_timestamp(self, val: str) -> str:
        """Normalize timestamp strings to a common format."""
        if val == "<NULL>":
            return val

        # Try common timestamp formats
        for fmt in [
            self.config.timestamp_format,
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d",
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%Y",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y",
        ]:
            try:
                ts = pd.to_datetime(val, format=fmt, errors="raise")
                # Strip sub-second precision for comparison
                return ts.strftime(self.config.timestamp_format)
            except (ValueError, TypeError):
                continue

        # Try pandas flexible parser as last resort
        try:
            ts = pd.to_datetime(val, errors="raise")
            return ts.strftime(self.config.timestamp_format)
        except (ValueError, TypeError):
            return val

    def _normalize_float(self, val: str) -> str:
        """Normalize numeric strings to a fixed precision."""
        if val == "<NULL>":
            return val
        try:
            f = float(val)
            return f"{f:.{self.config.float_precision}f}"
        except (ValueError, TypeError):
            return val

    # ──────────────────────────────────────────────
    #  Checksum
    # ──────────────────────────────────────────────

    @staticmethod
    def _compute_checksum(df: pd.DataFrame) -> str:
        """
        Compute a SHA-256 checksum of the DataFrame for quick comparison.

        Reuses the pattern from ``bo-bq-migrator/src/validator.py``.
        """
        if df.empty:
            return hashlib.sha256(b"empty").hexdigest()

        # Sort rows for deterministic comparison
        sorted_df = df.astype(str)
        data = json.dumps(
            {
                "columns": list(sorted_df.columns),
                "rows": sorted_df.values.tolist(),
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(data.encode()).hexdigest()

    # ──────────────────────────────────────────────
    #  Sample diff collection
    # ──────────────────────────────────────────────

    def _collect_sample_diffs(
        self,
        df1: pd.DataFrame,
        df2: pd.DataFrame,
        cols: list[str],
    ) -> list[dict[str, Any]]:
        """Collect the first N rows where values differ."""
        diffs: list[dict[str, Any]] = []
        max_display = self.config.max_diff_rows_display

        min_len = min(len(df1), len(df2))
        for i in range(min_len):
            if len(diffs) >= max_display:
                break
            row1 = df1.iloc[i]
            row2 = df2.iloc[i]
            row_diff: dict[str, Any] = {"row_index": i, "columns": {}}
            for col in cols:
                v1 = str(row1[col])
                v2 = str(row2[col])
                if v1 != v2:
                    row_diff["columns"][col] = {"as_is": v1, "to_be": v2}
            if row_diff["columns"]:
                diffs.append(row_diff)

        return diffs