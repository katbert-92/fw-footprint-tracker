"""Panel definitions for a project dashboard.

Series names come from the SQL `metric` column, so none of the panels need the
displayName gymnastics a schemaless backend forces on you.
"""

import re

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


def bargauge_usage(variant_tags: list, region_limits: dict) -> dict:
    # byRegexp, not byName: the series is "REGION · branch", so an exact match
    # would stop finding it and every region would fall back to the panel's
    # default thresholds.
    overrides = [
        {
            "matcher": {"id": "byRegexp", "options": f"^{re.escape(region)}( ·.*)?$"},
            "properties": [{"id": "thresholds", "value": _threshold_steps(thresholds)}],
        }
        for region, (_, thresholds) in sorted(region_limits.items())
        if thresholds
    ]

    return {
        "type": "bargauge",
        "title": "Region usage, last build — $area",
        "datasource": DS,
        "gridPos": {"h": 8, "w": 24, "x": 0, "y": 0},
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


def row_per_area() -> dict:
    return {
        "type": "row",
        "title": "Memory area: $area",
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": 8},
        # Collapsed: the gauges above answer "is anything running out", and the
        # history is opened when the answer is yes.
        "collapsed": True,
        "repeat": "area",
        "repeatDirection": "v",
        "panels": [],
    }


def timeseries_trend(variant_tags: list) -> dict:
    return {
        "type": "timeseries",
        "title": "Usage over time — $area",
        "datasource": DS,
        "gridPos": {"h": 10, "w": 24, "x": 0, "y": 9},
        "targets": _target(queries.trend(variant_tags))
        + _target(queries.area_capacity(variant_tags), ref="B"),
        "options": {
            "legend": {
                "displayMode": "table",
                "placement": "right",
                "showLegend": True,
                # lastNotNull, not last: two branches share one time axis, so a
                # branch with no build at the newest timestamp has a null there
                # and "last" would show it an empty column.
                "calcs": ["lastNotNull", "min", "max"],
                "sortBy": "Name",
                "sortDesc": True,
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "fieldConfig": {
            "defaults": {
                # One shared axis in bytes. Per-field limits would make Grafana
                # split the series onto separate axes, where two lines drawn at
                # different scales look parallel however far apart they are.
                "unit": "bytes",
                # A region moves by hundreds of bytes at a time, and whole KiB
                # round several builds into the same number.
                "decimals": 1,
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2,
                    "pointSize": 5,
                    "showPoints": "always",
                    "spanNulls": False,
                    "fillOpacity": 36,
                    "gradientMode": "opacity",
                    "lineInterpolation": "smooth",
                    "lineStyle": {"fill": "dot", "dash": [0, 20]},
                    "stacking": {"mode": "none", "group": "A"},
                },
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "Capacity"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}},
                        {"id": "custom.lineStyle", "value": {"fill": "dot", "dash": [0, 10]}},
                        {"id": "custom.fillOpacity", "value": 0},
                        {"id": "custom.lineWidth", "value": 2},
                        {"id": "custom.showPoints", "value": "always"},
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
                "decimals": 1,
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
    """Bytes per region per build, one bar group per build.

    A barchart rather than a time series: the x axis here is the build, not the
    calendar. Builds come in bursts and then nothing for a day, and on a time
    axis that reads as a wall of bars followed by emptiness.
    """
    return {
        "type": "barchart",
        "title": "By build — $area",
        "datasource": DS,
        "gridPos": {"h": 10, "w": 24, "x": 0, "y": 19},
        "targets": _target(queries.by_build(variant_tags)),
        "options": {
            "orientation": "auto",
            "showValue": "always",
            "stacking": "none",
            "barRadius": 0.1,
            "barWidth": 0.87,
            "groupWidth": 0.88,
            "fullHighlight": False,
            "xTickLabelRotation": 0,
            # Without spacing every build gets a label and they overlap into a
            # grey smear.
            "xTickLabelSpacing": 100,
            "legend": {
                "displayMode": "table",
                "placement": "right",
                "showLegend": True,
                "calcs": ["lastNotNull", "min", "max"],
            },
            "tooltip": {"mode": "single"},
        },
        "fieldConfig": {
            "defaults": {
                "unit": "bytes",
                "decimals": 1,
                "custom": {"fillOpacity": 100, "lineWidth": 0},
            },
            "overrides": [],
        },
    }


def barchart_delta(variant_tags: list) -> dict:
    return _bars(
        "Delta vs previous build — $area",
        queries.delta_by_build(variant_tags),
        {"h": 8, "w": 24, "x": 0, "y": 29},
    )


# ── Activity dashboard ──────────────────────────────────────────────────────


ALL_BRANCHES = " · all branches"


def _scope(pins: dict) -> str:
    """Title suffix naming the slice a panel is pinned to.

    This dashboard mixes two scopes -- the counting panels see every branch,
    the memory panels only the pinned one -- and without a suffix the reader
    has to remember which panel is which.
    """
    return f" · {pins['branch']}" if pins.get("branch") else ALL_BRANCHES


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


def _bars_by_category(title: str, sql: str, grid: dict) -> dict:
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
            "showValue": "always",
            "xTickLabelRotation": 0,
            "barRadius": 0.1,
            "barWidth": 0.83,
            "legend": {"showLegend": False},
            "tooltip": {"mode": "single"},
        },
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "continuous-viridis"},
                "custom": {
                    "fillOpacity": 80,
                    "lineWidth": 0,
                    "axisSoftMin": 0,
                    "gradientMode": "scheme",
                },
            },
            "overrides": [],
        },
    }


def stat_activity_totals() -> dict:
    return {
        "type": "stat",
        "title": "Common" + ALL_BRANCHES,
        "datasource": DS,
        "gridPos": {"h": 7, "w": 14, "x": 0, "y": 0},
        "targets": _target(queries.activity_totals(), table=True),
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/", "values": False},
            "orientation": "vertical",
            "justifyMode": "center",
            "textMode": "auto",
            "colorMode": "none",
            "graphMode": "area",
        },
        "fieldConfig": {
            "defaults": {"decimals": 0, "color": {"mode": "fixed", "fixedColor": "text"}},
            "overrides": [],
        },
    }


def gauge_area_totals(variant_tags: list, pins: dict) -> dict:
    """One dial per memory area: how full it is as a whole, on the latest build."""
    return {
        "type": "gauge",
        "title": "Memory areas, last build" + _scope(pins),
        "datasource": DS,
        "gridPos": {"h": 7, "w": 10, "x": 14, "y": 0},
        "targets": _target(queries.area_totals(variant_tags, pins)),
        "options": {
            "orientation": "vertical",
            "shape": "gauge",
            "barShape": "rounded",
            "barWidthFactor": 0.3,
            "segmentCount": 1,
            "segmentSpacing": 0.3,
            "endpointMarker": "glow",
            "effects": {"barGlow": True, "centerGlow": True, "gradient": True},
            "minVizHeight": 75,
            "minVizWidth": 75,
            "sizing": "auto",
            "sparkline": False,
            "textMode": "value_and_name",
            "showThresholdLabels": False,
            "showThresholdMarkers": False,
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
            "overrides": [],
        },
    }


def timeseries_builds_over_time() -> dict:
    return {
        "type": "timeseries",
        "title": "Builds over time" + ALL_BRANCHES,
        "datasource": DS,
        "gridPos": {"h": 9, "w": 14, "x": 0, "y": 7},
        "targets": _target(queries.builds_over_time()),
        "options": {
            "legend": {"showLegend": True},
            "tooltip": {"mode": "single"},
        },
        "fieldConfig": {
            "defaults": {
                "decimals": 0,
                "color": {"mode": "continuous-viridis"},
                "custom": {
                    "drawStyle": "bars",
                    "fillOpacity": 55,
                    "lineWidth": 2,
                    "showPoints": "auto",
                    "axisSoftMin": 0,
                    "gradientMode": "scheme",
                    "lineInterpolation": "smooth",
                    "barWidthFactor": 0.7,
                    "showValues": True,
                },
            },
            "overrides": [],
        },
    }


def barchart_by_hour() -> dict:
    return _bars_by_category(
        "By hour of day" + ALL_BRANCHES,
        queries.builds_by_hour(),
        {"h": 7, "w": 14, "x": 0, "y": 23},
    )


def barchart_by_weekday() -> dict:
    return _bars_by_category(
        "By weekday" + ALL_BRANCHES,
        queries.builds_by_weekday(),
        {"h": 7, "w": 14, "x": 0, "y": 16},
    )


def table_punchcard() -> dict:
    """Weekday against hour, coloured like a contribution graph."""
    return {
        "type": "table",
        "title": "When builds happen" + ALL_BRANCHES,
        "datasource": DS,
        "gridPos": {"h": 8, "w": 14, "x": 0, "y": 30},
        "targets": _target(queries.builds_by_weekday_and_hour(), table=True),
        "options": {"showHeader": True, "cellHeight": "sm"},
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "align": "center",
                    "cellOptions": {"type": "color-background", "mode": "gradient"},
                    "width": 44,
                },
                "color": {"mode": "continuous-viridis"},
                "min": 0,
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "day"},
                    "properties": [
                        {"id": "custom.cellOptions", "value": {"type": "auto"}},
                        {"id": "custom.width", "value": 60},
                        {"id": "custom.align", "value": "left"},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "total"},
                    "properties": [
                        {"id": "custom.cellOptions", "value": {"type": "auto"}},
                        {"id": "custom.width", "value": 70},
                    ],
                },
            ],
        },
    }


def table_authors() -> dict:
    return _table(
        "Who builds" + ALL_BRANCHES,
        queries.builds_by("author"),
        {"h": 8, "w": 8, "x": 0, "y": 38},
    )


def table_branches() -> dict:
    # No scope suffix on this one or the next: a panel that lists branches, or
    # where builds came from, says what it covers by listing it.
    return _table(
        "Busiest branches",
        queries.builds_by("branch"),
        {"h": 8, "w": 8, "x": 8, "y": 38},
    )


def table_origins() -> dict:
    return _table("Where from", queries.builds_by("origin"), {"h": 8, "w": 8, "x": 16, "y": 38})


def timeseries_fullness(variant_tags: list, pins: dict, areas: list) -> dict:
    """Every measurement, per area: percent on the left axis, bytes on the right.

    Two axes rather than two panels: "45%" and "how many KiB is that" are the
    same question asked twice, and the point is to read both off one line.
    """
    percent = "^(" + "|".join(re.escape(area) for area in sorted(areas)) + ")$"
    return {
        "type": "timeseries",
        "title": "How full, per memory area" + _scope(pins),
        "datasource": DS,
        "gridPos": {"h": 22, "w": 10, "x": 14, "y": 16},
        "targets": _target(queries.fullness_over_time(variant_tags, pins))
        + _target(queries.fullness_bytes_over_time(variant_tags, pins), ref="B"),
        "options": {
            "legend": {
                "displayMode": "table",
                "placement": "bottom",
                "showLegend": True,
                "calcs": ["lastNotNull", "max"],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "fieldConfig": {
            "defaults": {
                # The defaults describe the bytes series and the override the
                # percentages, not the other way round: only the percentages
                # have a fixed scale, and a default of 0-100 would flatten
                # every byte count against the top of the panel.
                "unit": "bytes",
                "decimals": 1,
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 3,
                    "fillOpacity": 0,
                    "lineInterpolation": "smooth",
                    "lineStyle": {"fill": "dot", "dash": [0, 10]},
                    "showPoints": "always",
                    "showValues": False,
                    "pointSize": 5,
                    "spanNulls": True,
                    "axisPlacement": "right",
                    "thresholdsStyle": {"mode": "dashed"},
                },
                "color": {"mode": "palette-classic"},
                # A base step only. The levels belong to the percentages; left
                # here they would draw a dashed warning line at 80 bytes.
                "thresholds": _threshold_steps([]),
            },
            "overrides": [
                {
                    "matcher": {"id": "byRegexp", "options": percent},
                    "properties": [
                        {"id": "unit", "value": "percent"},
                        {"id": "min", "value": 0},
                        {"id": "max", "value": 100},
                        {"id": "custom.axisPlacement", "value": "left"},
                        {"id": "thresholds", "value": _threshold_steps([75, 85, 95])},
                    ],
                }
            ],
        },
    }


def table_tightest_regions(variant_tags: list, pins: dict) -> dict:
    """Which regions to worry about."""
    return _table(
        "Tightest regions" + _scope(pins),
        queries.tightest_regions(variant_tags, pins),
        {"h": 9, "w": 10, "x": 14, "y": 7},
        overrides=[
            {
                "matcher": {"id": "byName", "options": "Peak %"},
                "properties": [
                    {"id": "unit", "value": "percent"},
                    {"id": "max", "value": 100},
                    {"id": "min", "value": 0},
                    {"id": "thresholds", "value": _threshold_steps([75, 85, 95])},
                    {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "gradient"}},
                ],
            },
        ],
    )
