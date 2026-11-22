"""The part of fwtrack-tags that touches files rather than the database."""

from fwtrack import tags


def dashboards(tmp_path, monkeypatch, *extra):
    folder = tmp_path / "blinky"
    folder.mkdir()
    for name in ("fwtrack-blinky.json", "fwtrack-blinky-activity.json", *extra):
        (folder / name).write_text("{}")
    monkeypatch.setenv("FWTRACK_DASHBOARDS_DIR", str(tmp_path))
    return folder


def test_dashboards_are_looked_for_where_the_ingest_service_writes_them(tmp_path, monkeypatch):
    folder = dashboards(tmp_path, monkeypatch)

    assert {path.name for path in tags.dashboards_of("blinky")} == {
        "fwtrack-blinky.json",
        "fwtrack-blinky-activity.json",
    }
    assert folder.is_dir()


def test_without_the_directory_setting_nothing_is_found(tmp_path, monkeypatch):
    dashboards(tmp_path, monkeypatch)
    monkeypatch.delenv("FWTRACK_DASHBOARDS_DIR")

    assert tags.dashboards_of("blinky") == []


def test_removing_them_leaves_what_was_drawn_by_hand(tmp_path, monkeypatch):
    folder = dashboards(tmp_path, monkeypatch, "mine.json")

    tags.remove_dashboards(tags.dashboards_of("blinky"))

    assert [path.name for path in folder.iterdir()] == ["mine.json"]


def test_the_folder_goes_when_it_is_left_empty(tmp_path, monkeypatch):
    folder = dashboards(tmp_path, monkeypatch)

    tags.remove_dashboards(tags.dashboards_of("blinky"))

    assert not folder.exists()
