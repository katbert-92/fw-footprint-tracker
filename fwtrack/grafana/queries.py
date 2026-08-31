"""SQL behind the dashboard panels.

Two shapes matter, and mixing them up is the classic way to get a panel that
returns correct rows and still renders wrong:

  * series panels (time series, stat, bar gauge) want a `time` column, a
    numeric `value` and a string `metric` that names the series;
  * table panels (the build list, the bar charts) want one flat row set, where
    the series are the columns themselves.

Region columns in the bar charts are emitted per region rather than pivoted
dynamically, because SQL cannot name columns at run time. The generator asks the
database which regions an area has and writes them out, which means a region
newly added to a linker script needs the dashboard regenerated.
"""

# Shown for builds recorded before a dimension existed, or after a project
# stopped recording it. Without it those builds match no value of the filter and
# vanish from every panel with nothing to say why; as a value of its own it is
# selectable, and the gap in the history is visible instead of silent.
NO_VALUE = "(none)"


def _dimension(tag: str) -> str:
    return f"COALESCE(tags->>'{tag}', '{NO_VALUE}')"

# Seconds are not decoration: local rebuilds of a dirty tree share a commit and
# land within the same minute, and without them every bar collapses into one
# category.


def _filters(variant_tags: list, time_filter: bool = True) -> str:
    """WHERE clause shared by every panel.

    Each variant tag is a single-value variable: mixing optimisation levels on
    one chart is meaningless, the difference between profiles dwarfs any feature.

    time_filter is dropped by the panels that look backwards: a window has to
    contain the build being compared, not the one it is compared against.
    """
    lines = ["  WHERE project = '$project'"]
    lines += [f"    AND {_dimension(tag)} = '${tag}'" for tag in variant_tags]
    lines += [
        "    AND branch IN (${branch:sqlstring})",
        "    AND origin IN (${origin:sqlstring})",
        "    AND COALESCE(author, '(none)') IN (${author:sqlstring})",
        # Regions are filtered here rather than by clicking the legend, so the
        # choice sticks across panels and reloads. The capacity line follows
        # suit: the ceiling of what is on screen, not of what was left out.
        "    AND region IN (${region:sqlstring})",
    ]
    if time_filter:
        lines.append("    AND $__timeFilter(built_at)")

    return "\n".join(lines)


def trend(variant_tags: list) -> str:
    """Bytes used over time, one series per region and branch.

    branch belongs in the series name: without it points from different branches
    merge into a single line that jumps between them.
    """
    return f"""SELECT built_at AS time,
       used AS value,
       region || ' · ' || branch AS metric
FROM memory_points
{_filters(variant_tags)}
    AND area = '$area'
ORDER BY 1"""


def area_capacity(variant_tags: list) -> str:
    """Total size of the area, as a series so it labels itself.

    A panel threshold cannot serve here: the panel is repeated across areas and
    its configuration is shared, while the capacity differs for each. Coming
    from the query it follows the repeat on its own.
    """
    return f"""SELECT built_at AS time,
       SUM(total) AS value,
       'Capacity' AS metric
FROM memory_points
{_filters(variant_tags)}
    AND area = '$area'
GROUP BY built_at
ORDER BY 1"""


def usage_gauge(variant_tags: list) -> str:
    """How full each region is, as of its most recent build.

    DISTINCT ON keeps one row per region rather than letting the panel reduce a
    whole history to its last point: with several branches selected those
    histories interleave, and "the last value" would silently be whichever
    branch happened to build most recently. One row per region, and the branch
    in the series name, so the gauge says whose measurement it is showing.
    """
    return f"""SELECT time, value, metric
FROM (
  SELECT DISTINCT ON (region) built_at AS time,
         pcnt AS value,
         region || ' · ' || branch AS metric
  FROM memory_points
{_filters(variant_tags)}
    AND area = '$area'
  ORDER BY region, built_at DESC
) latest
ORDER BY 1"""


def _delta_cte(variant_tags: list, area_filter: bool = True) -> str:
    """Change against the previous build of the same region on the same branch.

    Partitioning by branch as well as region matters: comparing a commit on one
    branch against the previous build on another produces a number that means
    nothing.

    Computed over the whole history and filtered by time afterwards. With the
    range applied here instead, the build a change is measured against would be
    cut off whenever it fell outside the window -- and since most builds are the
    only one of their branch and variant on a given day, the panel came out
    empty exactly when it was most needed.
    """
    area = "\n    AND area = '$area'" if area_filter else ""
    return f"""WITH deltas AS (
  SELECT build_id,
         built_at,
         commit,
         region,
         branch,
         used - LAG(used) OVER (PARTITION BY region, branch ORDER BY built_at) AS delta
  FROM memory_points
{_filters(variant_tags, time_filter=False)}{area}
)"""


def toolchain_changes(variant_tags: list) -> str:
    """When the compiler changed, as a Grafana annotation.

    A value that changes twice a year has no business being a column on a
    thousand rows. As a mark on the time axis it answers the question it is
    actually asked -- "everything grew here, did we change compiler?" -- on the
    chart where the jump is seen, rather than in a table somewhere else.

    LAG runs over the whole history and the time filter is applied after it, so
    a change is still reported when the build before it falls outside the range.
    Partitioned by variant: different platforms may well be built with different
    toolchains, and ordering those into one sequence would mark every build as a
    change.
    """
    conditions = ["project = '$project'", "toolchain IS NOT NULL"]
    conditions += [f"{_dimension(tag)} = '${tag}'" for tag in variant_tags]

    return f"""SELECT built_at AS time,
       'Toolchain: ' || toolchain AS text
FROM (
  SELECT built_at,
         toolchain,
         LAG(toolchain) OVER (ORDER BY built_at) AS previous
  FROM builds
  WHERE {" AND ".join(conditions)}
) changes
WHERE previous IS DISTINCT FROM toolchain
  AND previous IS NOT NULL
  AND $__timeFilter(built_at)
ORDER BY 1"""


def by_build(variant_tags: list) -> str:
    """Bytes per region per build.

    Long format, one row per region: a wide one would have to name its columns
    in SQL, and since the panel is repeated across areas with a single query
    that means every region of the project appears on every area's chart.
    """
    return f"""SELECT built_at AS time,
       used AS value,
       region AS metric
FROM memory_points
{_filters(variant_tags)}
    AND area = '$area'
ORDER BY 1"""


def delta_by_build(variant_tags: list) -> str:
    return f"""{_delta_cte(variant_tags)}
SELECT built_at AS time,
       delta AS value,
       region AS metric
FROM deltas
WHERE delta IS NOT NULL
  AND $__timeFilter(built_at)
ORDER BY 1"""


def variable_values(tag: str, depends_on: list) -> str:
    """Values of one build dimension, narrowed by the dimensions before it.

    Without the narrowing every variable is independent, each picks its own
    first value, and their combination is easily one that never existed: cfg=1
    means -O1 while opt=0 sits next to it. The dashboard then shows No data with
    no hint as to why.
    """
    conditions = ["project = '$project'", "$__timeFilter(built_at)"]
    conditions += [f"{_dimension(name)} = '${name}'" for name in depends_on]

    return (
        f"SELECT DISTINCT {_dimension(tag)} AS value\n"
        "FROM builds\n"
        f"WHERE {' AND '.join(conditions)}\n"
        "ORDER BY 1"
    )


def author_values() -> str:
    """Authors seen in the range, with builds that have none kept selectable."""
    return (
        "SELECT DISTINCT COALESCE(author, '(none)') AS value\n"
        "FROM builds\n"
        "WHERE project = '$project'\n"
        "  AND $__timeFilter(built_at)\n"
        "ORDER BY 1"
    )


def simple_values(column: str, table: str = "memory_points") -> str:
    """Values of a plain column, limited to the dashboard's time range.

    The time filter is what keeps this usable on a long-lived project: without
    it every branch that ever existed stays in the dropdown for ever, and
    picking a dead one shows an empty dashboard that looks broken rather than
    finished.
    """
    return (
        f"SELECT DISTINCT {column} AS value\n"
        f"FROM {table}\n"
        "WHERE project = '$project'\n"
        "  AND $__timeFilter(built_at)\n"
        "ORDER BY 1"
    )


# ── Activity ────────────────────────────────────────────────────────────────
#
# A second dashboard, about the flow of builds rather than about memory: when
# they happen, who makes them, and how much room is left. It carries the same
# filters as the memory dashboard -- without them the memory panels would be
# adding up boards that have nothing in common.


# Two filters, because this dashboard answers two questions.
#
# How much work is happening is a question about the project: narrowing it to
# one variant and one branch turns "1500 builds" into "59" and answers nothing
# anybody asked. How much memory is left is the opposite -- a bootloader on one
# board and an application on another have different memories, and a number
# spanning both is meaningless.
#
# No region filter in either: this dashboard has no region variable, and every
# panel here is about whole areas.

FLOW_FILTER = "  WHERE project = '$project'\n    AND $__timeFilter(built_at)"

# Columns of `builds` that can be pinned like a dimension can.
PINNABLE_COLUMNS = ("branch", "origin", "version", "toolchain")


def _literal(value: str) -> str:
    """A string safe to paste into generated SQL."""
    return "'" + value.replace("'", "''") + "'"


def _memory_filter(variant_tags: list, pins: dict | None = None) -> str:
    """Filters for the panels about how much memory is left.

    A pinned dimension is written into the query instead of becoming a
    dropdown. Most projects have a slice they always mean -- the trunk branch,
    the production tag, the application rather than the bootloader -- and
    turning each of those into a filter makes the reader choose them again
    every time, and lets them choose a combination nobody wanted.

    What is not pinned stays a filter, which is how a project keeps the one
    dimension it does want to flip between.
    """
    pins = pins or {}
    lines = ["  WHERE project = '$project'"]
    for tag in variant_tags:
        value = f"{_literal(pins[tag])}" if tag in pins else f"'${tag}'"
        lines.append(f"    AND {_dimension(tag)} = {value}")

    lines += [
        f"    AND {column} = {_literal(pins[column])}"
        for column in PINNABLE_COLUMNS
        if column in pins
    ]
    lines.append("    AND $__timeFilter(built_at)")
    return "\n".join(lines)


def activity_totals() -> str:
    """One row of counters for the stat panel."""
    return f"""SELECT count(*)                  AS "Builds",
       count(DISTINCT commit)         AS "Commits",
       count(DISTINCT branch)         AS "Branches",
       count(DISTINCT COALESCE(author, '(none)')) AS "Authors"
FROM builds
{FLOW_FILTER}"""


def builds_over_time() -> str:
    """How many builds landed, bucketed to whatever the time range deserves.

    $__interval rather than a fixed hour: two days of history want hourly bars,
    a quarter wants daily ones, and a fixed bucket is wrong for one of them.
    """
    return f"""SELECT $__timeGroup(built_at, $__interval) AS time,
       count(*) AS value,
       'builds' AS metric
FROM builds
{FLOW_FILTER}
GROUP BY 1
ORDER BY 1"""


def builds_by_hour() -> str:
    """Which hours of the day builds land in, local to the database."""
    return f"""SELECT to_char(built_at, 'HH24') AS hour,
       count(*) AS builds
FROM builds
{FLOW_FILTER}
GROUP BY 1
ORDER BY 1"""


def builds_by_weekday() -> str:
    # Grouped by the number as well so the days come out in week order rather
    # than alphabetically; a bar chart keeps the row order it is given.
    return f"""SELECT to_char(built_at, 'Dy') AS day,
       count(*) AS builds
FROM builds
{FLOW_FILTER}
GROUP BY 1, extract(isodow from built_at)
ORDER BY extract(isodow from built_at)"""


def builds_by(column: str, limit: int = 15) -> str:
    """Who or what most of the builds come from.

    Commits as well as builds: one push fans out into a build per variant, so
    a builds column on its own reads as if someone had built dozens of times.
    """
    return f"""SELECT COALESCE({column}, '(none)') AS name,
       count(DISTINCT commit) AS commits,
       count(*) AS builds
FROM builds
{FLOW_FILTER}
GROUP BY 1
ORDER BY 3 DESC
LIMIT {int(limit)}"""


def builds_by_weekday_and_hour() -> str:
    """The two bar charts crossed: a row per weekday, a column per hour.

    A column per hour rather than an hour column and a count: a table panel
    colours cells, so the grid is the heatmap and no plugin has to be installed.
    """
    hours = "\n".join(
        f'       count(*) FILTER (WHERE extract(hour from built_at) = {h}) AS "{h:02d}",'
        for h in range(24)
    )
    return f"""SELECT to_char(built_at, 'Dy') AS day,
{hours}
       count(*) AS total
FROM builds
{FLOW_FILTER}
GROUP BY 1, extract(isodow from built_at)
ORDER BY extract(isodow from built_at)"""


def fullness_over_time(variant_tags: list, pins: dict | None = None) -> str:
    """Every measurement of how full each area is, one point per build.

    Nothing is folded away: the regions of an area are summed, which is what
    "per area" means and what the bar gauge above the panel shows, and each
    build keeps its own point. Bucketing by day and taking the worst region --
    what this did before -- hid both a second build on the same day and any
    area whose tight region never moves.
    """
    return f"""SELECT built_at AS time,
       area AS metric,
       round(100.0 * sum(used) / sum(total), 1) AS value
FROM memory_points
{_memory_filter(variant_tags, pins)}
    AND total > 0
GROUP BY 1, 2
ORDER BY 1"""


def fullness_bytes_over_time(variant_tags: list, pins: dict | None = None) -> str:
    """The same measurements in bytes, for the second axis of the same panel."""
    return f"""SELECT built_at AS time,
       area || ' · used' AS metric,
       sum(used) AS value
FROM memory_points
{_memory_filter(variant_tags, pins)}
    AND total > 0
GROUP BY 1, 2
ORDER BY 1"""


def tightest_regions(variant_tags: list, pins: dict | None = None, limit: int = 12) -> str:
    """The regions closest to their ceiling."""
    return f"""SELECT area AS "Area",
       region AS "Region",
       round(max(pcnt), 1) AS "Peak %",
       round(min(total - used) / 1024.0, 1) AS "Free KiB",
       count(DISTINCT build_id) AS "Builds"
FROM memory_points
{_memory_filter(variant_tags, pins)}
    AND pcnt IS NOT NULL
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT {int(limit)}"""


def area_totals(variant_tags: list, pins: dict | None = None) -> str:
    """How full each memory area is as a whole, on the most recent build.

    Regions of an area are summed within one build and never across builds: two
    builds of the same variant are two measurements of the same memory, not
    twice as much of it.
    """
    return f"""WITH per_build AS (
  SELECT area,
         built_at,
         sum(used) AS used,
         sum(total) AS total
  FROM memory_points
{_memory_filter(variant_tags, pins)}
    AND total IS NOT NULL
  GROUP BY area, built_at
)
SELECT DISTINCT ON (area)
       built_at AS time,
       round(100.0 * used / total, 1) AS value,
       area AS metric
FROM per_build
WHERE total > 0
ORDER BY area, built_at DESC
"""
