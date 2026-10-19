import pytest

from fwtrack import config


def test_named_config_must_exist(tmp_path):
    with pytest.raises(FileNotFoundError):
        config.find_config(tmp_path / "missing.toml")


def test_tags_override_in_order(monkeypatch):
    settings = {"meta": {"tags": ["board"]}}
    monkeypatch.setenv("FWTRACK_TAGS", "board=env,opt=s")

    tags = config.collect_custom_tags({"board": "meta"}, settings, ["opt=cli"])

    assert tags == {"board": "env", "opt": "cli"}


@pytest.mark.parametrize("name", ["branch", "author", "datasource", "region"])
def test_tags_cannot_shadow_core_fields(monkeypatch, name):
    monkeypatch.delenv("FWTRACK_TAGS", raising=False)

    assert config.collect_custom_tags({}, {}, [f"{name}=x"]) == {}


@pytest.mark.parametrize("pair", ["board-rev=a", "board.rev=a", "board=" + "x" * 201])
def test_tags_a_dashboard_cannot_use_are_dropped(monkeypatch, pair):
    monkeypatch.delenv("FWTRACK_TAGS", raising=False)

    assert config.collect_custom_tags({}, {}, [pair]) == {}


def test_most_specific_threshold_wins():
    settings = {
        "defaults": {"thresholds": [70]},
        "group": [{"name": "flash", "match": ["FLASH*"], "thresholds": [90, 80]}],
        "region": {"FLASH_BOOT": {"thresholds": [50]}},
    }

    assert config.resolve_thresholds("FLASH_BOOT", "flash", settings) == [50]
    assert config.resolve_thresholds("FLASH", "flash", settings) == [80, 90]
    assert config.resolve_thresholds("RAM", "other", settings) == [70]


def test_unmatched_region_is_still_recorded():
    assert config.resolve_area("NEW_REGION", {"group": []}) == config.FALLBACK_AREA


def test_dashboard_settings_come_from_the_project():
    assert config.dashboard_settings({}) is None

    settings = config.dashboard_settings(
        {
            "dashboard": {
                "main_branch": "dev",
                "filters": ["tag", "board-rev"],
                "overview": {"tag": "prd", "type": "app"},
            }
        }
    )

    assert settings == {
        "main_branch": "dev",
        "variant_tags": ["tag"],
        "overview_pins": ["tag=prd", "type=app"],
    }
    # A section left empty puts every setting back to its default.
    assert config.dashboard_settings({"dashboard": {}}) == dict.fromkeys(settings)
