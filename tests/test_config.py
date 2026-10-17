import pytest

from fwtrack import config


def test_tags_override_in_order(monkeypatch):
    settings = {"meta": {"tags": ["board"]}}
    monkeypatch.setenv("FWTRACK_TAGS", "board=env,opt=s")

    tags = config.collect_custom_tags({"board": "meta"}, settings, ["opt=cli"])

    assert tags == {"board": "env", "opt": "cli"}


@pytest.mark.parametrize("name", ["branch", "region"])
def test_tags_cannot_shadow_core_fields(monkeypatch, name):
    monkeypatch.delenv("FWTRACK_TAGS", raising=False)

    assert config.collect_custom_tags({}, {}, [f"{name}=x"]) == {}


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
