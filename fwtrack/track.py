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

from dotenv import load_dotenv

from . import db
from .config import (
    DEFAULT_CONFIG_FILE,
    collect_custom_tags,
    load_config,
    resolve_area,
    resolve_thresholds,
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
    parser.add_argument(
        "-m", "--meta", type=Path, required=True, help="Build metadata JSON"
    )
    parser.add_argument(
        "-c", "--config", type=Path, default=DEFAULT_CONFIG_FILE, help="Tracking config"
    )
    parser.add_argument("--toolchain", default="", help="Toolchain string, from the ELF")
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


def resolve_timestamp(meta: dict, repo: Path) -> tuple:
    """When the build happened, and whether the tree was dirty.

    On a clean tree the commit time is used: re-running CI on the same commit
    then overwrites the same row instead of adding a duplicate, and the history
    tracks the code rather than the build queue. On a dirty tree the commit no
    longer describes the binary, so the build time is used and every local
    iteration stays its own point.
    """
    dirty = bool(git_output(repo, "status", "--porcelain", "--untracked-files=no"))

    if not dirty:
        commit_ts = git_output(repo, "show", "-s", "--format=%ct", "HEAD")
        if commit_ts.isdigit():
            return datetime.fromtimestamp(int(commit_ts), timezone.utc), False
        logger.warning("Could not read commit timestamp, falling back to build time")

    build_ts = meta.get("ts")
    if isinstance(build_ts, int):
        return datetime.fromtimestamp(build_ts, timezone.utc), dirty

    logger.warning("No usable 'ts' in build metadata, falling back to the current time")
    return datetime.now(timezone.utc), dirty


def build_record(meta: dict, config: dict, cli_tags: list, toolchain: str, repo: Path) -> dict:
    built_at, dirty = resolve_timestamp(meta, repo)

    return {
        "project": str(meta["prj"]),
        "built_at": built_at,
        "commit": str(meta.get("hash", "")),
        "branch": str(meta.get("branch", "")),
        "version": f"v{meta.get('major', 0)}.{meta.get('minor', 0)}.{meta.get('build', 0)}",
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
    load_dotenv()  # the environment wins over .env: an explicit switch must work

    if os.getenv(ENABLE_ENV, "0").lower() not in TRUTHY:
        logger.info(f"{ENABLE_ENV} is not set, skipping upload")
        return

    meta = read_json(args.meta)
    usage = read_json(args.input)
    config = load_config(args.config)

    build = build_record(meta, config, args.tag, args.toolchain, args.repo)
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
