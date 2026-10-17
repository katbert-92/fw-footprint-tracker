import re

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
