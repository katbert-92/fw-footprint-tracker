import pytest

from fwtrack import cli


@pytest.fixture
def project(tmp_path, v1, monkeypatch):
    monkeypatch.delenv("FWTRACK_ENABLE", raising=False)
    config = tmp_path / "fw_tracking.toml"
    config.write_text(
        f"""
project = "fixture"

[analyse]
elf = "{v1[0]}"
map = "{v1[1]}"

[[group]]
name = "ram"
match = ["RAM", "BACKUP"]
thresholds = [0.1, 0.2]
"""
    )
    return config


def test_without_enable_nothing_is_recorded(project, capsys):
    assert cli.run(config=project, read_dotenv=False) is None
    assert "FLASH" in capsys.readouterr().out


def test_dry_run_shows_what_would_be_sent(project, monkeypatch, capsys):
    monkeypatch.setenv("FWTRACK_ENABLE", "1")

    assert cli.run(config=project, read_dotenv=False, dry_run=True, branch="dev") is None

    out = capsys.readouterr().out
    assert '"project": "fixture"' in out and '"area": "ram"' in out
