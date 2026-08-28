"""Panel definitions for a project dashboard.

Series names come from the SQL `metric` column, so none of the panels need the
displayName gymnastics a schemaless backend forces on you.
"""

from . import queries

DS = {"type": "grafana-postgresql-datasource", "uid": "${datasource}"}

THRESHOLD_COLOURS = ["#EAB839", "orange", "red", "dark-red"]


def _target(sql: str, table: bool = False, ref: str = "A") -> list:
    return [
        {
            "datasource": DS,
            "refId": ref,
            "format": "table" if table else "time_series",
            "rawQuery": True,
            "rawSql": sql,
            "editorMode": "code",
        }
    ]


def _threshold_steps(values: list, base: str = "green") -> dict:
    """Grafana colour bands from a list of levels: [85, 90, 99] gives four."""
    steps = [{"color": base, "value": None}]
    for i, value in enumerate(values):
        colour = THRESHOLD_COLOURS[min(i, len(THRESHOLD_COLOURS) - 1)]
        steps.append({"color": colour, "value": value})

    return {"mode": "absolute", "steps": steps}


def stat_last_delta(variant_tags: list, width: int) -> dict:
    return {
        "type": "stat",
        "title": "Last build delta — $area",
        "datasource": DS,
        "gridPos": {"h": 5, "w": width, "x": 0, "y": 0},
        "repeat": "area",
        "repeatDirection": "h",
        "targets": _target(queries.last_delta(variant_tags)),
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "orientation": "horizontal",
            "textMode": "value_and_name",
            "colorMode": "value",
            "graphMode": "none",
        },
        "fieldConfig": {
            "defaults": {
                "unit": "bytes",
                "decimals": 0,
                "color": {"mode": "thresholds"},
                # Growth is red, shrinking is green, unchanged is neutral.
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "green", "value": None},
                        {"color": "text", "value": 0},
                        {"color": "red", "value": 1},
                    ],
                },
            },
            "overrides": [],
        },
    }


def bargauge_usage(variant_tags: list, width: int, region_limits: dict) -> dict:
    overrides = [
        {
            "matcher": {"id": "byName", "options": region},
            "properties": [{"id": "thresholds", "value": _threshold_steps(thresholds)}],
        }
        for region, (_, thresholds) in sorted(region_limits.items())
        if thresholds
    ]

    return {
        "type": "bargauge",
        "title": "Region usage — $area",
        "datasource": DS,
        "gridPos": {"h": 8, "w": width, "x": 0, "y": 5},
        "repeat": "area",
        "repeatDirection": "h",
        "targets": _target(queries.usage_gauge(variant_tags)),
        "options": {
            "displayMode": "gradient",
            "orientation": "horizontal",
            "showUnfilled": True,
            "valueMode": "color",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        "fieldConfig": {
            "defaults": {
                "unit": "percent",
                "min": 0,
                "max": 100,
                "decimals": 1,
                "thresholds": _threshold_steps([75, 85, 95]),
            },
            "overrides": overrides,
        },
    }


def table_builds(variant_tags: list) -> dict:
    def rename(column: str, label: str) -> dict:
        return {
            "matcher": {"id": "byName", "options": column},
            "properties": [{"id": "displayName", "value": label}],
        }

    return {
        "type": "table",
        "title": "Builds: date, commit, region, size",
        "datasource": DS,
        "gridPos": {"h": 8, "w": 24, "x": 0, "y": 13},
        "targets": _target(queries.builds_table(variant_tags), table=True),
        "options": {"showHeader": True, "sortBy": [{"displayName": "Date", "desc": True}]},
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto", "filterable": True}},
            "overrides": [
                rename("time", "Date"),
                rename("commit", "Commit"),
                rename("branch", "Branch"),
                rename("author", "Author"),
                rename("version", "Version"),
                rename("dirty", "Uncommitted"),
                rename("area", "Area"),
                rename("region", "Region"),
                {
                    "matcher": {"id": "byName", "options": "used"},
                    "properties": [
                        {"id": "unit", "value": "bytes"},
                        {"id": "displayName", "value": "Used"},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "pcnt"},
                    "properties": [
                        {"id": "unit", "value": "percent"},
                        {"id": "decimals", "value": 1},
                        {"id": "min", "value": 0},
                        {"id": "max", "value": 100},
                        {"id": "displayName", "value": "Usage"},
                        {
                            "id": "custom.cellOptions",
                            "value": {"type": "gauge", "mode": "gradient"},
                        },
                    ],
                },
            ],
        },
    }


def row_per_area() -> dict:
    return {
        "type": "row",
        "title": "Memory area: $area",
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": 21},
        "collapsed": False,
        "repeat": "area",
        "repeatDirection": "v",
        "panels": [],
    }


def timeseries_trend(variant_tags: list) -> dict:
    return {
        "type": "timeseries",
        "title": "Usage over time — $area",
        "datasource": DS,
        "gridPos": {"h": 10, "w": 14, "x": 0, "y": 22},
        "targets": _target(queries.trend(variant_tags))
        + _target(queries.area_capacity(variant_tags), ref="B"),
        "options": {
            "legend": {
                "displayMode": "table",
                "placement": "right",
                "showLegend": True,
                "calcs": ["last", "min", "max"],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "fieldConfig": {
            "defaults": {
                # One shared axis in bytes. Per-field limits would make Grafana
                # split the series onto separate axes, where two lines drawn at
                # different scales look parallel however far apart they are.
                "unit": "bytes",
                "min": 0,
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2,
                    "pointSize": 6,
                    "showPoints": "always",
                    "spanNulls": True,
                    "fillOpacity": 25,
                    "lineInterpolation": "stepAfter",
                    "stacking": {"mode": "normal", "group": "A"},
                },
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "Capacity"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}},
                        {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                        {"id": "custom.fillOpacity", "value": 0},
                        {"id": "custom.lineWidth", "value": 2},
                        {"id": "custom.showPoints", "value": "never"},
                        # Kept out of the stack: it is the ceiling the stack is
                        # measured against, not another slice of it.
                        {"id": "custom.stacking", "value": {"mode": "none"}},
                    ],
                }
            ],
        },
    }


def _bars(title: str, sql: str, grid: dict) -> dict:
    """Bars over time rather than a bar chart panel.

    The bar chart wants one column per series, which has to be written into the
    SQL; with the panel repeated across areas and a single shared query that
    puts every region of the project on every area's chart. A time series takes
    the series name from a column, so each repeat shows only its own regions.
    """
    return {
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "gridPos": grid,
        "targets": _target(sql),
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "fieldConfig": {
            "defaults": {
                "unit": "bytes",
                "custom": {
                    "drawStyle": "bars",
                    "fillOpacity": 85,
                    "lineWidth": 0,
                    "barAlignment": 0,
                    "showPoints": "never",
                    "axisSoftMin": 0,
                },
            },
            "overrides": [],
        },
    }


def barchart_by_build(variant_tags: list) -> dict:
    return _bars(
        "By build — $area",
        queries.by_build(variant_tags),
        {"h": 10, "w": 10, "x": 14, "y": 22},
    )


def barchart_delta(variant_tags: list) -> dict:
    return _bars(
        "Delta vs previous build — $area",
        queries.delta_by_build(variant_tags),
        {"h": 8, "w": 24, "x": 0, "y": 32},
    )


# ── Activity dashboard ──────────────────────────────────────────────────────


def _table(title: str, sql: str, grid: dict, overrides: list | None = None) -> dict:
    return {
        "type": "table",
        "title": title,
        "datasource": DS,
        "gridPos": grid,
        "targets": _target(sql, table=True),
        "options": {"showHeader": True},
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto", "filterable": True}},
            "overrides": overrides or [],
        },
    }


def _bars_by_category(title: str, sql: str, grid: dict, colour: str) -> dict:
    """Bar chart over a text column: hours, weekdays, names.

    A barchart rather than a time series: the x axis here is a category, and the
    order is the one the query returns.
    """
    return {
        "type": "barchart",
        "title": title,
        "datasource": DS,
        "gridPos": grid,
        "targets": _target(sql, table=True),
        "options": {
            "orientation": "auto",
            "showValue": "auto",
            "xTickLabelRotation": 0,
            "legend": {"showLegend": False},
            "tooltip": {"mode": "single"},
        },
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "fixed", "fixedColor": colour},
                "custom": {"fillOpacity": 80, "lineWidth": 0, "axisSoftMin": 0},
            },
            "overrides": [],
        },
    }


def stat_activity_totals() -> dict:
    return {
        "type": "stat",
        "title": "In the range",
        "datasource": DS,
        "gridPos": {"h": 4, "w": 24, "x": 0, "y": 0},
        "targets": _target(queries.activity_totals(), table=True),
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "orientation": "horizontal",
            "textMode": "value_and_name",
            "colorMode": "value",
            "graphMode": "none",
        },
        "fieldConfig": {
            "defaults": {
                "decimals": 0,
                "color": {"mode": "fixed", "fixedColor": "text"},
            },
            "overrides": [],
        },
    }


def timeseries_builds_per_day() -> dict:
    return {
        "type": "timeseries",
        "title": "Builds per day",
        "datasource": DS,
        "gridPos": {"h": 8, "w": 24, "x": 0, "y": 4},
        "targets": _target(queries.builds_per_day()),
        "options": {
            "legend": {"showLegend": False},
            "tooltip": {"mode": "single"},
        },
        "fieldConfig": {
            "defaults": {
                "decimals": 0,
                "color": {"mode": "fixed", "fixedColor": "blue"},
                "custom": {
                    "drawStyle": "bars",
                    "fillOpacity": 80,
                    "lineWidth": 0,
                    "showPoints": "never",
                    "axisSoftMin": 0,
                },
            },
            "overrides": [],
        },
    }


def barchart_by_hour() -> dict:
    return _bars_by_category(
        "By hour of day", queries.builds_by_hour(), {"h": 7, "w": 12, "x": 0, "y": 12}, "purple"
    )


def barchart_by_weekday() -> dict:
    return _bars_by_category(
        "By weekday", queries.builds_by_weekday(), {"h": 7, "w": 12, "x": 12, "y": 12}, "green"
    )


def table_authors() -> dict:
    return _table("Who builds", queries.builds_by("author"), {"h": 8, "w": 8, "x": 0, "y": 19})


def table_branches() -> dict:
    return _table(
        "Busiest branches", queries.builds_by("branch"), {"h": 8, "w": 8, "x": 8, "y": 19}
    )


def table_origins() -> dict:
    return _table("Where from", queries.builds_by("origin"), {"h": 8, "w": 8, "x": 16, "y": 19})


def table_commits() -> dict:
    """The build log. The commit hash is the column this panel exists for."""
    def rename(column: str, label: str) -> dict:
        return {
            "matcher": {"id": "byName", "options": column},
            "properties": [{"id": "displayName", "value": label}],
        }

    return _table(
        "Builds, newest first",
        queries.recent_commits(),
        {"h": 12, "w": 24, "x": 0, "y": 27},
        overrides=[
            rename("time", "Date"),
            rename("commit", "Commit"),
            rename("branch", "Branch"),
            rename("author", "Author"),
            rename("version", "Version"),
            rename("origin", "Origin"),
            rename("dirty", "Uncommitted"),
            {
                "matcher": {"id": "byName", "options": "total_used"},
                "properties": [
                    {"id": "unit", "value": "bytes"},
                    {"id": "displayName", "value": "Total used"},
                ],
            },
        ],
    )
