"""Project configuration: memory areas, warning thresholds and custom tags.

Everything here is project specific. The tool itself only ever sets the core
fields the dashboards are built on -- project, region, area, commit, branch,
version, origin, dirty -- and lets a project declare any other dimension it
wants to slice by.
"""

import fnmatch
import json
import os
import re
import tomllib
from pathlib import Path

from .log import get_logger

logger = get_logger(__name__)

# Looked for in order, so a project can keep the config at its root or tuck it
# in with the rest of the build system.
CONFIG_CANDIDATES = [
    Path("fw_tracking.toml"),
    Path("build/fw_tracking.toml"),
    Path(".config/fw_tracking.toml"),
]
DEFAULT_ELF = Path("fw.elf")
DEFAULT_MAP = Path("fw.map")
FALLBACK_AREA = "other"

# Applied when the config says nothing. Chosen to be noticeable but not noisy:
# a region past 75% is worth watching, past 95% is nearly out of room.
FALLBACK_THRESHOLDS = [75, 85, 95]

# Set by the tool, never by a project. A custom tag using one of these names is
# dropped rather than silently shadowing an axis or a legend -- or a dashboard
# variable, which shares the namespace: hence datasource.
CORE_FIELDS = frozenset(
    {
        "project",
        "region",
        "area",
        "commit",
        "branch",
        "version",
        "origin",
        "dirty",
        "author",
        "toolchain",
        "datasource",
    }
)

# Every tag becomes a Grafana variable, and Grafana only recognises $name when
# name is made of word characters: board-rev would reach the SQL as the text
# "$board-rev" and match nothing.
TAG_NAME = re.compile(r"[A-Za-z0-9_]+")
MAX_TAG_VALUE = 200


def valid_tag(key: str, value: str) -> bool:
    return bool(TAG_NAME.fullmatch(key)) and 0 < len(value) <= MAX_TAG_VALUE


def find_config(explicit: Path | None = None) -> Path | None:
    """Locate the tracking config, so the usual case needs no arguments.

    One that was named outright has to exist: falling back to defaults would
    turn a typo in a CI script into a build recorded under the wrong settings.
    """
    if explicit:
        if not explicit.is_file():
            raise FileNotFoundError(f"Config not found: {explicit}")
        return explicit

    return next((p for p in CONFIG_CANDIDATES if p.is_file()), None)


def load_config(path: Path | None) -> dict:
    if path is None:
        logger.warning(
            f"No config found ({', '.join(str(p) for p in CONFIG_CANDIDATES)}), using defaults"
        )
        return {}

    with path.open("rb") as f:
        return tomllib.load(f)


def resolve_area(region: str, config: dict) -> str:
    """Map a linker region onto a user-defined area, first match wins.

    A region matching no group still gets recorded, under FALLBACK_AREA, so a
    newly added region in a linker script shows up instead of vanishing.
    """
    for group in config.get("group", []):
        if any(fnmatch.fnmatch(region, pat) for pat in group.get("match", [])):
            return group["name"]

    logger.warning(f"Region '{region}' matched no group, falling back to '{FALLBACK_AREA}'")
    return FALLBACK_AREA


def resolve_thresholds(region: str, area: str, config: dict) -> list:
    """Warning levels in percent, most specific setting wins.

    [region.NAME] beats the [[group]] the region belongs to, which beats
    [defaults]. The physical size of a region is never configured -- it comes
    from the MAP file. These are policy on top of it.
    """
    region_cfg = config.get("region", {}).get(region, {})
    if "thresholds" in region_cfg:
        return sorted(int(t) for t in region_cfg["thresholds"])

    for group in config.get("group", []):
        if group.get("name") == area and "thresholds" in group:
            return sorted(int(t) for t in group["thresholds"])

    defaults = config.get("defaults", {}).get("thresholds", FALLBACK_THRESHOLDS)
    return sorted(int(t) for t in defaults)


def parse_tag_pairs(pairs, source: str) -> dict:
    tags = {}
    for pair in pairs:
        # An empty entry is nothing, not a malformed tag: that is what an unset
        # FWTRACK_TAGS looks like ("".split(",") == [""]) and what a trailing
        # comma leaves behind.
        if not pair.strip():
            continue

        key, sep, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key or not value:
            logger.warning(f"Ignoring malformed tag from {source}: '{pair}' (expected KEY=VALUE)")
            continue

        tags[key] = value

    return tags


def load_meta(config: dict, override: Path | None = None) -> dict:
    """Build metadata produced by whatever build system this project uses.

    Entirely optional, and never a format this tool defines. Everything needed
    about a build -- commit, branch, time, dirtiness -- comes from git, and the
    project name from the config. This reads a file the project already writes,
    under whatever key names it already uses, purely to pick up dimensions worth
    slicing by. A project without one declares its tags with --tag or
    FWTRACK_TAGS, or has none at all.

    JSON or TOML.
    """
    path = override or config.get("meta", {}).get("file")
    if not path:
        return {}

    path = Path(path)
    if not path.is_file():
        logger.warning(f"Metadata file not found: {path}, continuing without it")
        return {}

    if path.suffix == ".toml":
        with path.open("rb") as f:
            return tomllib.load(f)

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def resolve_version(meta: dict, config: dict) -> str | None:
    """Firmware version, if this project has one.

    Deliberately not a tag: every tag becomes a single-select dashboard filter,
    and being forced to pick one version would hide exactly the trend across
    versions the charts exist to show. It behaves like the branch instead --
    something you look through, not something you filter down to.
    """
    literal = config.get("version")
    if literal:
        return str(literal)

    key = config.get("meta", {}).get("version")
    if key and key in meta:
        return str(meta[key])

    return None


def resolve_project(meta: dict, config: dict) -> str | None:
    if "project" in config:
        return str(config["project"])

    key = config.get("meta", {}).get("project")
    if key and key in meta:
        return str(meta[key])

    return None


def collect_custom_tags(meta: dict, config: dict, cli_tags: list) -> dict:
    """Custom dimensions from the config, then the environment, then arguments.

    Each source overrides the previous one so a one-off build can be labelled
    without editing the config.
    """
    tags = {}

    for key in config.get("meta", {}).get("tags", []):
        if key in meta:
            tags[key] = str(meta[key])
        else:
            # Loud on purpose: a declared dimension missing from a build makes
            # that build invisible under a dashboard filter on it, with nothing
            # to show why.
            logger.warning(
                f"Tag '{key}' is declared in the config but missing from the build metadata; "
                "builds without it will not show under a filter on it"
            )

    tags.update(parse_tag_pairs(os.getenv("FWTRACK_TAGS", "").split(","), "FWTRACK_TAGS"))
    tags.update(parse_tag_pairs(cli_tags, "--tag"))

    for key in sorted(set(tags) & CORE_FIELDS):
        logger.warning(f"Custom tag '{key}' collides with a core field and is ignored")
        del tags[key]

    for key in sorted(k for k, v in tags.items() if not valid_tag(k, v)):
        logger.warning(
            f"Custom tag '{key}' is ignored: names are letters, digits and underscores, "
            f"values 1 to {MAX_TAG_VALUE} characters"
        )
        del tags[key]

    return tags


def dashboard_settings(config: dict) -> dict | None:
    """[dashboard] of the project config, in the shape the server stores it.

    None without the section, and the server keeps what it has. A section that
    is there is the whole truth: a setting left out of it is back to its default.
    """
    section = config.get("dashboard")
    if section is None:
        return None

    filters = [str(f).strip() for f in section.get("filters") or []]
    overview = {str(k).strip(): str(v).strip() for k, v in (section.get("overview") or {}).items()}
    unusable = [f for f in filters if not TAG_NAME.fullmatch(f)]
    unusable += [k for k, v in overview.items() if not valid_tag(k, v)]
    if unusable:
        logger.warning(f"[dashboard] ignores what no tag can be called: {', '.join(unusable)}")

    return {
        "main_branch": str(section.get("main_branch") or "").strip() or None,
        "variant_tags": [f for f in filters if f not in unusable] or None,
        "overview_pins": [f"{k}={v}" for k, v in overview.items() if k not in unusable] or None,
    }
