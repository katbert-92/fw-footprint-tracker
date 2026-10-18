import json
import re
from argparse import Namespace

from fwtrack.grafana import gen_dashboard, queries

# A variable pasted in raw: '$name' or '${name}' inside quotes, or $name next
# to an operator. Grafana substitutes those as they are, from the page URL.
RAW_VARIABLE = re.compile(r"'\$\{?\w+\}?'|=\s*\$(?!__)\w+\b")


def all_sql(dashboard: dict) -> list:
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "rawSql":
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(dashboard)
    return found


def dashboards(variant_tags=("board", "cfg"), pins=None, areas=("flash", "ram")):
    regions = gen_dashboard.build_dashboard("blinky", list(variant_tags), {}, "uid", "dev")
    overview = gen_dashboard.build_activity_dashboard(
        "blinky", list(variant_tags), "uid", list(areas), pins or {}
    )
    return regions, overview


def test_no_variable_reaches_the_sql_unquoted():
    for dashboard in dashboards(pins={"branch": "dev", "board": "o'brien"}):
        for sql in all_sql(dashboard):
            assert not RAW_VARIABLE.search(sql), sql


def test_pinned_values_are_escaped():
    sql = queries.variable_values("cfg", ["board"], {"tag": "o'k"})

    assert "'o''k'" in sql


def test_a_project_without_dimensions_has_no_dimension_filters():
    regions, overview = dashboards(variant_tags=())

    names = [v["name"] for v in regions["templating"]["list"]]
    assert names == ["datasource", "project", "branch", "origin", "author", "area", "region"]
    assert [v["name"] for v in overview["templating"]["list"]] == ["datasource", "project"]


def test_deltas_compare_builds_of_the_same_branch():
    assert "PARTITION BY branch, area" in queries.delta_per_build(["cfg"], {}, ["flash"])


def test_generate_writes_both_dashboards(tmp_path, monkeypatch):
    class Stub:
        pass

    stored = {}
    db = gen_dashboard.db
    monkeypatch.setattr(db, "discover_variant_tags", lambda c, p: ["cfg", "bad-name"])
    monkeypatch.setattr(db, "discover_areas", lambda c, p: ["flash"])
    monkeypatch.setattr(db, "region_limits", lambda c, p: {"FLASH": (4096, [80, 90])})
    monkeypatch.setattr(db, "save_project_settings", lambda c, p, **v: stored.update(v))
    monkeypatch.setattr(db, "reset_project_settings", lambda c, p, names: None)
    monkeypatch.setattr(db, "project_settings", lambda c, p: {})

    args = Namespace(
        out_dir=tmp_path,
        variant_tags="",
        overview_pin="",
        main_branch="",
        exclude_tags="",
        datasource_uid="fwtrack-postgres",
        push=False,
        folder_uid=None,
        reset=[],
    )
    gen_dashboard.generate(Stub(), "blinky", args)

    written = sorted(p.name for p in (tmp_path / "blinky").iterdir())
    assert written == ["fwtrack-blinky-activity.json", "fwtrack-blinky.json"]
    variables = [
        v["name"]
        for v in json.loads((tmp_path / "blinky" / "fwtrack-blinky.json").read_text())[
            "templating"
        ]["list"]
    ]
    assert "cfg" in variables and "bad-name" not in variables
