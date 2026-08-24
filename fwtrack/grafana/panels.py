"""Panel definitions for a project dashboard.

Series names come from the SQL `metric` column, so none of the panels need the
displayName gymnastics a schemaless backend forces on you.
"""

from . import queries

DS = {"type": "grafana-postgresql-datasource", "uid": "${datasource}"}

THRESHOLD_COLOURS = ["#EAB839", "orange", "red", "dark-red"]


def _target(sql: str, table: bool = False) -> list:
    return [
        {
            "datasource": DS,
            "refId": "A",
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
        steps.append({"color": THRESHOLD_COLOURS[min(i, len(THRESHOLD_COLOURS) - 1)], "value": value})

    return {"mode": "absolute", "steps": steps}


def _region_threshold_overrides(region_limits: dict) -> list:
    """Dashed lines on the byte axis, one set per region.

    Thresholds are stored as percentages, but the trend is plotted in bytes, so
    they are converted here using the region size recorded with the latest
    build. A linker script change moves the size, and the dashboard has to be
    regenerated for the lines to follow.
    """
    overrides = []
    for region, (total, thresholds) in sorted(region_limits.items()):
        if not thresholds or not total:
            continue

        absolute = [round(total * pct / 100) for pct in thresholds]
        overrides.append(
            {
                "matcher": {"id": "byRegexp", "options": f"^{region} · .*"},
                "properties": [
                    {"id": "thresholds", "value": _threshold_steps(absolute)},
                    {"id": "custom.thresholdsStyle", "value": {"mode": "dashed"}},
                    {"id": "max", "value": total},
                ],
            }
        )

    return overrides


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
                rename("version", "Version"),
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


def timeseries_trend(variant_tags: list, region_limits: dict) -> dict:
    return {
        "type": "timeseries",
        "title": "Trend over time — $area",
        "datasource": DS,
        "gridPos": {"h": 10, "w": 14, "x": 0, "y": 22},
        "targets": _target(queries.trend(variant_tags)),
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
                "unit": "bytes",
                "min": 0,
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2,
                    "pointSize": 6,
                    "showPoints": "always",
                    "spanNulls": True,
                    "fillOpacity": 8,
                    "lineInterpolation": "stepAfter",
                },
            },
            "overrides": _region_threshold_overrides(region_limits),
        },
    }


def barchart_by_build(variant_tags: list, regions: list) -> dict:
    return {
        "type": "barchart",
        "title": "By build (commit · date) — $area",
        "datasource": DS,
        "gridPos": {"h": 10, "w": 10, "x": 14, "y": 22},
        "targets": _target(queries.by_build(variant_tags, regions), table=True),
        "options": {
            "xField": "build",
            "orientation": "auto",
            "stacking": "normal",
            "showValue": "never",
            "xTickLabelRotation": -45,
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "fieldConfig": {
            "defaults": {"unit": "bytes", "custom": {"fillOpacity": 85, "lineWidth": 0}},
            "overrides": [],
        },
    }


def barchart_delta(variant_tags: list, regions: list) -> dict:
    return {
        "type": "barchart",
        "title": "Delta vs previous build — $area",
        "datasource": DS,
        "gridPos": {"h": 8, "w": 24, "x": 0, "y": 32},
        "targets": _target(queries.delta_by_build(variant_tags, regions), table=True),
        "options": {
            "xField": "build",
            "orientation": "auto",
            # Not stacked: deltas of opposite sign cancel each other out in a
            # stack and the bar stops meaning anything.
            "stacking": "none",
            "showValue": "auto",
            "xTickLabelRotation": -45,
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "fieldConfig": {
            "defaults": {"unit": "bytes", "custom": {"fillOpacity": 85, "lineWidth": 0}},
            "overrides": [],
        },
    }
