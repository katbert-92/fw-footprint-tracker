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

BUILD_LABEL = "commit || ' · ' || to_char(built_at, 'DD.MM HH24:MI:SS')"

# Seconds are not decoration: local rebuilds of a dirty tree share a commit and
# land within the same minute, and without them every bar collapses into one
# category.


def _filters(variant_tags: list) -> str:
    """WHERE clause shared by every panel.

    Each variant tag is a single-value variable: mixing optimisation levels on
    one chart is meaningless, the difference between profiles dwarfs any feature.
    """
    lines = ["  WHERE project = '$project'"]
    lines += [f"    AND tags->>'{tag}' = '${tag}'" for tag in variant_tags]
    lines += [
        "    AND branch IN (${branch:sqlstring})",
        "    AND origin IN (${origin:sqlstring})",
        "    AND $__timeFilter(built_at)",
    ]
    return "\n".join(lines)


def trend(variant_tags: list) -> str:
    """Usage over time, one series per region and branch.

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


def builds_table(variant_tags: list) -> str:
    return f"""SELECT built_at AS time,
       commit,
       branch,
       version,
       area,
       region,
       used,
       pcnt
FROM memory_points
{_filters(variant_tags)}
ORDER BY built_at DESC"""


def _region_columns(regions: list, expr: str) -> str:
    return ",\n".join(
        f"       MAX({expr}) FILTER (WHERE region = '{region}') AS \"{region}\""
        for region in regions
    )


def by_build(variant_tags: list, regions: list) -> str:
    """Absolute usage with the commit on the x axis.

    A categorical axis is more honest than a time axis when builds arrive
    unevenly: a quiet day, then five builds in an hour.
    """
    return f"""SELECT {BUILD_LABEL} AS build,
{_region_columns(regions, "used")}
FROM memory_points
{_filters(variant_tags)}
    AND area = '$area'
GROUP BY build_id, built_at, commit
ORDER BY built_at"""


def delta_by_build(variant_tags: list, regions: list) -> str:
    return f"""{_delta_cte(variant_tags)}
SELECT {BUILD_LABEL} AS build,
{_region_columns(regions, "delta")}
FROM deltas
WHERE delta IS NOT NULL
GROUP BY build_id, built_at, commit
ORDER BY built_at"""


def variable_values(tag: str, depends_on: list) -> str:
    """Values of one build dimension, narrowed by the dimensions before it.

    Without the narrowing every variable is independent, each picks its own
    first value, and their combination is easily one that never existed: cfg=1
    means -O1 while opt=0 sits next to it. The dashboard then shows No data with
    no hint as to why.
    """
    conditions = ["project = '$project'"]
    conditions += [f"tags->>'{name}' = '${name}'" for name in depends_on]

    return (
        f"SELECT DISTINCT tags->>'{tag}' AS value\n"
        "FROM builds\n"
        f"WHERE {' AND '.join(conditions)}\n"
        f"  AND tags ? '{tag}'\n"
        "ORDER BY 1"
    )


def simple_values(column: str, table: str = "memory_points") -> str:
    return (
        f"SELECT DISTINCT {column} AS value\n"
        f"FROM {table}\n"
        "WHERE project = '$project'\n"
        f"ORDER BY 1"
    )
