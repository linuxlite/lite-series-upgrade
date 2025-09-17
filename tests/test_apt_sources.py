from pathlib import Path

from lite_series_upgrade.apt_sources import should_backup_apt_source


class TestShouldBackupAptSource:
    def test_skips_main_sources_list(self) -> None:
        assert not should_backup_apt_source(Path("/etc/apt/sources.list"))

    def test_skips_sources_list_d_entries(self) -> None:
        assert not should_backup_apt_source(
            Path("/etc/apt/sources.list.d/linuxlite.list")
        )

    def test_other_paths_are_backed_up(self, tmp_path) -> None:
        other = tmp_path / "sources.list"
        assert should_backup_apt_source(other)
