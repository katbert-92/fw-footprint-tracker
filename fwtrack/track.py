"""Send the result of a footprint analysis to the database.

Off unless FWTRACK_ENABLE is set, so a plain local build stays offline and needs
no database credentials at all. Turn it on per build:

    FWTRACK_ENABLE=1 fwtrack-push -i fw_sections.json
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from . import db
from .config import (
    DEFAULT_CONFIG_FILE,
    collect_custom_tags,
    load_config,
    load_meta,
    resolve_area,
    resolve_project,
    resolve_thresholds,
    resolve_version,
)
from .log import get_logger, setup_logging

logger = get_logger(__name__)

ENABLE_ENV = "FWTRACK_ENABLE"
TRUTHY = ("1", "true", "yes", "on")


def parse_args():
    parser = argparse.ArgumentParser(description="Push firmware footprint to the database")
    parser.add_argument(
        "-i", "--input", type=Path, required=True, help="Region usage JSON from fwtrack-analyse"
    )
    parser.add_argument("-m", "--meta", type=Path, help="Build metadata JSON, if the project has one")
    parser.add_argument(
        "-c", "--config", type=Path, default=DEFAULT_CONFIG_FILE, help="Tracking config"
    )
    parser.add_argument(
        "--toolchain", default="", help="Override the toolchain recorded by fwtrack-analyse"
    )
    parser.add_argument(
        "-C", "--repo", type=Path, default=Path("."),
        help="Repository to read commit and dirty state from (default: current directory)",
    )
    parser.add_argument(
        "-t", "--tag", action="append", default=[], metavar="KEY=VALUE",
        help="Custom tag, repeatable; overrides the config and FWTRACK_TAGS",
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="Print what would be written and stop"
    )

    return parser.parse_args()


def read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"File not found: {path}")
        sys.exit(1)
    except (IOError, json.JSONDecodeError) as e:
        logger.error(f"Error reading {path}: {e}")
        sys.exit(1)


def git_output(repo: Path, *args: str) -> str:
    """Run git against an explicit repository.

    Never relies on the current directory: the tool is installed as a package
    and may well be invoked from somewhere other than the project being
    measured, in which case picking up the wrong repository's commit would go
    unnoticed.
    """
    cmd = ["git", "-C", str(repo), *args]
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning(f"git command {' '.join(cmd)} failed: {e}")
        return ""


# Set by the CI system, in the order they should be trusted. A merge request
# build should be attributed to the branch being merged, not to the temporary
# ref the runner checked out.
CI_BRANCH_VARS = [
    "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME",  # GitLab, merge request pipelines
    "CI_COMMIT_BRANCH",                     # GitLab, branch pipelines
    "CI_COMMIT_REF_NAME",                   # GitLab, tags included
    "GITHUB_HEAD_REF",                      # GitHub, pull requests
    "GITHUB_REF_NAME",                      # GitHub, pushes
    "BRANCH_NAME",                          # Jenkins
]
DETACHED = "HEAD"


def resolve_branch(repo: Path, override: str | None = None) -> str:
    """Name of the branch this build belongs to.

    A CI runner checks out a commit rather than a branch, and git then reports
    the branch as 'HEAD'. Recording that loses the one dimension the whole
    comparison rests on, so the CI environment is consulted whenever git cannot
    name a branch.
    """
    if override:
        return override

    branch = git_output(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch and branch != DETACHED:
        return branch

    for name in CI_BRANCH_VARS:
        value = os.getenv(name)
        if value:
            logger.info(f"Detached HEAD: taking the branch from {name}")
            return value

    logger.warning(
        "Detached HEAD and no CI branch variable set; recording the branch as "
        f"'{DETACHED}'. Pass --branch to name it explicitly"
    )
    return DETACHED


def resolve_timestamp(repo: Path, dirty: bool):
    """When the build happened.

    On a clean tree the commit time is used: re-running CI on the same commit
    then overwrites the same row instead of adding a duplicate, and the history
    tracks the code rather than the build queue. On a dirty tree the commit no
    longer describes the binary, so the current time is used and every local
    iteration stays its own point.
    """
    if not dirty:
        commit_ts = git_output(repo, "show", "-s", "--format=%ct", "HEAD")
        if commit_ts.isdigit():
            return datetime.fromtimestamp(int(commit_ts), timezone.utc)
        logger.warning("Could not read commit timestamp, falling back to the current time")

    return datetime.now(timezone.utc)


def build_record(meta: dict, config: dict, cli_tags: list, toolchain: str,
                 repo: Path, project_override: str | None = None,
                 version_override: str | None = None,
                 branch_override: str | None = None) -> dict:
    """Everything about a build except the numbers.

    Only the tags come from the project's metadata file; the rest is read from
    git, so a project that writes no such file works just as well.
    """
    project = project_override or resolve_project(meta, config)
    if not project:
        logger.error("No project name: set `project` in the config or pass --project")
        sys.exit(1)

    dirty = bool(git_output(repo, "status", "--porcelain", "--untracked-files=no"))
    version = (
        version_override
        or resolve_version(meta, config)
        or git_output(repo, "describe", "--tags", "--abbrev=0")
    )

    return {
        "project": project,
        "built_at": resolve_timestamp(repo, dirty),
        "commit": git_output(repo, "rev-parse", "--short", "HEAD"),
        "branch": resolve_branch(repo, branch_override),
        "version": version or None,
        "origin": "ci" if os.getenv("CI") else "local",
        "dirty": dirty,
        "toolchain": toolchain or None,
        "tags": collect_custom_tags(meta, config, cli_tags),
    }


def region_records(usage: dict, config: dict) -> list:
    records = []
    for region, info in usage.items():
        area = resolve_area(region, config)
        records.append({
            "region": region,
            "area": area,
            "used": int(info["used"]),
            "total": int(info["total"]),
            "thresholds": resolve_thresholds(region, area, config),
        })

    return records


def main():
    setup_logging()
    args = parse_args()
    # usecwd: without it dotenv searches from this file, which once installed
    # means the package's own directory rather than the project being built.
    # The environment still wins over .env, so an explicit switch works.
    load_dotenv(find_dotenv(usecwd=True))

    if os.getenv(ENABLE_ENV, "0").lower() not in TRUTHY:
        logger.info(f"{ENABLE_ENV} is not set, skipping upload")
        return

    analysis = read_json(args.input)
    config = load_config(args.config)
    meta = load_meta(config, args.meta)

    usage = analysis["regions"]
    toolchain = args.toolchain or analysis.get("toolchain", "")

    build = build_record(meta, config, args.tag, toolchain, args.repo)
    regions = region_records(usage, config)

    if args.dry_run:
        print(json.dumps({"build": build, "regions": regions}, indent=2, default=str))
        logger.info(f"Dry run: {len(regions)} regions not written")
        return

    try:
        with db.connect() as conn:
            db.write_build(conn, build, regions)
    except Exception as e:
        logger.error(f"Failed to write to the database: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
