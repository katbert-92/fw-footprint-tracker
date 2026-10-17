from fwtrack.grafana import gen_dashboard, queries


def dashboards(variant_tags=("board", "cfg"), pins=None, areas=("flash", "ram")):
    regions = gen_dashboard.build_dashboard("blinky", list(variant_tags), {}, "uid", "dev")
    overview = gen_dashboard.build_activity_dashboard(
        "blinky", list(variant_tags), "uid", list(areas), pins or {}
    )
    return regions, overview


def test_pinned_values_are_escaped():
    sql = queries.variable_values("cfg", ["board"], {"tag": "o'k"})

    assert "'o''k'" in sql


def test_a_project_without_dimensions_has_no_dimension_filters():
    regions, overview = dashboards(variant_tags=())

    names = [v["name"] for v in regions["templating"]["list"]]
    assert names == ["datasource", "project", "branch", "origin", "author", "area", "region"]
    assert [v["name"] for v in overview["templating"]["list"]] == ["datasource", "project"]
