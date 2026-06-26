"""Tests for data_svc/providers/yahoo/parse.py — pure, no network."""

import pytest
import pandas as pd

from data_svc.providers.yahoo.parse import parse_chart

EXPECTED_COLUMNS = ["time", "open", "high", "low", "close", "volume"]


def _make_payload(timestamps, opens, highs, lows, closes, volumes):
    """Build a minimal Yahoo v8 chart payload."""
    return {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": opens,
                                "high": highs,
                                "low": lows,
                                "close": closes,
                                "volume": volumes,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


class TestParseChartValidPayload:
    def test_three_timestamps_one_null_close_returns_two_rows(self):
        """3 timestamps with 1 null close -> 2 rows, sorted ascending."""
        payload = _make_payload(
            timestamps=[1_700_000_000, 1_700_000_060, 1_700_000_120],
            opens=[100.0, 101.0, 102.0],
            highs=[105.0, 106.0, 107.0],
            lows=[99.0, 100.0, 101.0],
            closes=[104.0, None, 106.0],  # middle row has null close
            volumes=[1000, 2000, 3000],
        )

        df = parse_chart(payload)

        assert list(df.columns) == EXPECTED_COLUMNS
        assert len(df) == 2

    def test_correct_ohlcv_values_after_null_drop(self):
        """The surviving rows have the correct OHLCV values."""
        payload = _make_payload(
            timestamps=[1_700_000_000, 1_700_000_060, 1_700_000_120],
            opens=[100.0, 101.0, 102.0],
            highs=[105.0, 106.0, 107.0],
            lows=[99.0, 100.0, 101.0],
            closes=[104.0, None, 106.0],
            volumes=[1000, 2000, 3000],
        )

        df = parse_chart(payload)

        assert df.iloc[0]["time"] == 1_700_000_000
        assert df.iloc[0]["open"] == 100.0
        assert df.iloc[0]["high"] == 105.0
        assert df.iloc[0]["low"] == 99.0
        assert df.iloc[0]["close"] == 104.0
        assert df.iloc[0]["volume"] == 1000

        assert df.iloc[1]["time"] == 1_700_000_120
        assert df.iloc[1]["close"] == 106.0

    def test_sorted_ascending_by_time(self):
        """Result is sorted ascending by time."""
        payload = _make_payload(
            timestamps=[1_700_000_120, 1_700_000_000, 1_700_000_060],
            opens=[102.0, 100.0, 101.0],
            highs=[107.0, 105.0, 106.0],
            lows=[101.0, 99.0, 100.0],
            closes=[106.0, 104.0, 105.0],
            volumes=[3000, 1000, 2000],
        )

        df = parse_chart(payload)

        times = list(df["time"])
        assert times == sorted(times)


class TestParseChartErrorPayload:
    def test_chart_error_non_null_returns_empty_df(self):
        """payload with chart.error non-null -> empty df with correct columns."""
        payload = {
            "chart": {
                "result": None,
                "error": {"code": "Not Found", "description": "No data found"},
            }
        }

        df = parse_chart(payload)

        assert list(df.columns) == EXPECTED_COLUMNS
        assert len(df) == 0

    def test_empty_dict_returns_empty_df(self):
        """Empty dict -> empty df with correct columns."""
        df = parse_chart({})

        assert list(df.columns) == EXPECTED_COLUMNS
        assert len(df) == 0

    def test_none_returns_empty_df(self):
        """None -> empty df with correct columns."""
        df = parse_chart(None)

        assert list(df.columns) == EXPECTED_COLUMNS
        assert len(df) == 0

    def test_missing_result_returns_empty_df(self):
        """payload with result=None -> empty df."""
        payload = {"chart": {"result": None, "error": None}}

        df = parse_chart(payload)

        assert list(df.columns) == EXPECTED_COLUMNS
        assert len(df) == 0


class TestParseChartRaggedPayload:
    """R2: ragged (mismatched-length) arrays must return empty df — never raise."""

    def test_ragged_timestamps_vs_ohlcv_returns_empty_df(self):
        """timestamps has 3 elements but OHLCV arrays have 2 -> empty df, no raise."""
        payload = _make_payload(
            timestamps=[1_700_000_000, 1_700_000_060, 1_700_000_120],  # 3
            opens=[100.0, 101.0],   # 2
            highs=[105.0, 106.0],   # 2
            lows=[99.0, 100.0],     # 2
            closes=[104.0, 105.0],  # 2
            volumes=[1000, 2000],   # 2
        )

        df = parse_chart(payload)

        assert list(df.columns) == EXPECTED_COLUMNS
        assert len(df) == 0

    def test_ragged_volume_vs_prices_returns_empty_df(self):
        """volume list is shorter than other arrays -> empty df, no raise."""
        payload = _make_payload(
            timestamps=[1_700_000_000, 1_700_000_060],
            opens=[100.0, 101.0],
            highs=[105.0, 106.0],
            lows=[99.0, 100.0],
            closes=[104.0, 105.0],
            volumes=[1000],  # only 1 element, others have 2
        )

        df = parse_chart(payload)

        assert list(df.columns) == EXPECTED_COLUMNS
        assert len(df) == 0

    def test_ragged_does_not_raise(self):
        """parse_chart with ragged arrays must not raise any exception."""
        payload = _make_payload(
            timestamps=[1_700_000_000, 1_700_000_060, 1_700_000_120],
            opens=[100.0],
            highs=[105.0, 106.0],
            lows=[99.0],
            closes=[104.0, 105.0],
            volumes=[1000, 2000],
        )

        # Must not raise
        try:
            result = parse_chart(payload)
        except Exception as exc:
            pytest.fail(f"parse_chart raised unexpectedly: {exc}")

        assert list(result.columns) == EXPECTED_COLUMNS
