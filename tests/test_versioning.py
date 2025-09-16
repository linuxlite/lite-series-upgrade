import pytest

from lite_series_upgrade.versioning import (
    Series,
    SeriesFormatError,
    compute_upgrade_path,
    sort_series,
)


class TestSortSeries:
    def test_numeric_ordering(self) -> None:
        series = ["6.2", "10.0", "6.0"]
        assert sort_series(series) == ["6.0", "6.2", "10.0"]

    def test_suffixes_are_ignored(self) -> None:
        series = ["6.2 Beta", "10.0 (LTS)", "6.0 Final"]
        assert sort_series(series) == ["6.0 Final", "6.2 Beta", "10.0 (LTS)"]

    def test_error_for_non_numeric_labels(self) -> None:
        with pytest.raises(SeriesFormatError):
            sort_series(["series", "release"])


class TestComputeUpgradePath:
    def test_plans_path_to_latest(self) -> None:
        available = ["5.8", "6.0", "6.2", "10.0"]
        # 10.0 must appear last even though lexicographically it sorts before 6.x
        assert compute_upgrade_path("6.0", available) == ["6.2", "10.0"]

    def test_includes_current_when_requested(self) -> None:
        available = ["5.8", "6.0", "6.2", "10.0"]
        assert compute_upgrade_path("6.0", available, include_current=True) == [
            "6.0",
            "6.2",
            "10.0",
        ]

    def test_stops_at_target(self) -> None:
        available = ["5.8", "6.0", "6.2", "7.0", "10.0", "12.0"]
        assert compute_upgrade_path("6.0", available, target_series="10.0") == [
            "6.2",
            "7.0",
            "10.0",
        ]

    def test_raises_when_target_is_missing(self) -> None:
        available = ["5.8", "6.0", "6.2", "7.0"]
        with pytest.raises(ValueError):
            compute_upgrade_path("6.0", available, target_series="10.0")

    def test_raises_when_target_is_older(self) -> None:
        available = ["5.8", "6.0", "6.2", "7.0"]
        with pytest.raises(ValueError):
            compute_upgrade_path("7.0", available, target_series="6.2")


class TestSeriesDataclass:
    def test_version_key_matches_sorting(self) -> None:
        series = [Series("6.2"), Series("10.0"), Series("6.0")] 
        sorted_series = sorted(series, key=lambda s: s.version_key())
        assert [s.label for s in sorted_series] == ["6.0", "6.2", "10.0"]

