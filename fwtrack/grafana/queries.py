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


def _filters(variant_tags: list) -> str:
    """WHERE clause shared by every panel.

    Each variant tag is a single-value variable: mixing optimisation levels on
    one chart is meaningless, the difference between profiles dwarfs any feature.
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
        "    AND $__timeFilter(built_at)",
    ]
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
    return f"""SELECT built_at AS time,
       pcnt AS value,
       region AS metric
FROM memory_points
{_filters(variant_tags)}
    AND area = '$area'
ORDER BY 1"""


def _delta_cte(variant_tags: list, area_filter: bool = True) -> str:
    """Change against the previous build of the same region on the same branch.

    Partitioning by branch as well as region matters: comparing a commit on one
    branch against the previous build on another produces a number that means
    nothing.
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
{_filters(variant_tags)}{area}
)"""


def last_delta(variant_tags: list) -> str:
    """What the most recent build cost, one value per region.

    Deliberately the latest build only, rather than one series per branch: the
    panel answers "what did the build that just landed cost", and fanning it out
    across branches turns three readable numbers into a dozen unreadable ones.
    The branch is implied -- it is whichever one that build was on.
    """
    return f"""{_delta_cte(variant_tags)},
latest AS (
  SELECT max(built_at) AS built_at
  FROM deltas
  WHERE delta IS NOT NULL
)
SELECT deltas.built_at AS time,
       deltas.delta AS value,
       deltas.region AS metric
FROM deltas
JOIN latest ON deltas.built_at = latest.built_at
WHERE deltas.delta IS NOT NULL
ORDER BY 1"""


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


def builds_table(variant_tags: list) -> str:
    return f"""SELECT built_at AS time,
       commit,
       branch,
       author,
       version,
       dirty,
       area,
       region,
       used,
       pcnt
FROM memory_points
{_filters(variant_tags)}
ORDER BY built_at DESC"""


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
# they happen, who makes them, what landed. Deliberately not filtered by the
# variant dimensions -- the question here is "what is going on in this project",
# and splitting it per build variant would answer a different one.

ACTIVITY_FILTER = "WHERE project = '$project'\n  AND $__timeFilter(built_at)"


def activity_totals() -> str:
    """One row of counters for the stat panel."""
    return f"""SELECT count(*)                  AS "Builds",
       count(DISTINCT commit)         AS "Commits",
       count(DISTINCT branch)         AS "Branches",
       count(DISTINCT COALESCE(author, '(none)')) AS "Authors"
FROM builds
{ACTIVITY_FILTER}"""


def builds_per_day() -> str:
    return f"""SELECT date_trunc('day', built_at) AS time,
       count(*) AS value,
       'builds' AS metric
FROM builds
{ACTIVITY_FILTER}
GROUP BY 1
ORDER BY 1"""


def builds_by_hour() -> str:
    """Which hours of the day builds land in, local to the database."""
    return f"""SELECT to_char(built_at, 'HH24') AS hour,
       count(*) AS builds
FROM builds
{ACTIVITY_FILTER}
GROUP BY 1
ORDER BY 1"""


def builds_by_weekday() -> str:
    # Grouped by the number as well so the days come out in week order rather
    # than alphabetically; a bar chart keeps the row order it is given.
    return f"""SELECT to_char(built_at, 'Dy') AS day,
       count(*) AS builds
FROM builds
{ACTIVITY_FILTER}
GROUP BY 1, extract(isodow from built_at)
ORDER BY extract(isodow from built_at)"""


def builds_by(column: str, limit: int = 15) -> str:
    """Who or what most of the builds come from."""
    return f"""SELECT COALESCE({column}, '(none)') AS name,
       count(*) AS builds
FROM builds
{ACTIVITY_FILTER}
GROUP BY 1
ORDER BY 2 DESC
LIMIT {int(limit)}"""


def recent_commits() -> str:
    """The build log: one row per build, newest first, with its total footprint.

    The hash is the point of this panel -- it is what gets pasted into a message
    asking someone what they changed.
    """
    return """SELECT b.built_at AS time,
       b.commit,
       b.branch,
       COALESCE(b.author, '(none)') AS author,
       b.version,
       b.origin,
       b.dirty,
       sum(m.used) AS total_used
FROM builds b
LEFT JOIN memory_usage m ON m.build_id = b.id
WHERE b.project = '$project'
  AND $__timeFilter(b.built_at)
GROUP BY b.id, b.built_at, b.commit, b.branch, b.author, b.version, b.origin, b.dirty
ORDER BY b.built_at DESC
LIMIT 200"""
