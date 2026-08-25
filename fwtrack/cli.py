"""One command per build: analyse the artefacts, print the table, record it.

Paths live in fw_tracking.toml so the build system only has to say `fwtrack`.
Anything on the command line overrides the config, for one-off runs.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from . import db
from .analyse import analyse
from .config import (
    DEFAULT_ELF,
    DEFAULT_MAP,
    find_config,
    load_config,
    load_meta,
)
from .log import get_logger, setup_logging
from .track import ENABLE_ENV, TRUTHY, build_record, region_records

logger = get_logger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyse a firmware build and record its memory footprint"
    )
    parser.add_argument(
        "-c", "--config", type=Path,
        help="Tracking config (default: fw_tracking.toml, build/fw_tracking.toml)",
    )
    parser.add_argument("-e", "--elf", type=Path, help="ELF file, overriding the config")
    parser.add_argument("-m", "--map", type=Path, help="MAP file, overriding the config")
    parser.add_argument("--meta", type=Path, help="Build metadata JSON, overriding the config")
    parser.add_argument("--project", help="Project name, overriding the config")
    parser.add_argument("--version", help="Firmware version, overriding the config")
    parser.add_argument(
        "--branch", help="Branch name, for a CI checkout git cannot name itself"
    )
    parser.add_argument(
        "-o", "--output", type=Path, help="Also write the analysis to this JSON file"
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
        "-n", "--dry-run", action="store_true", help="Print what would be recorded and stop"
    )

    return parser.parse_args()


def run(
    config: Path | str | None = None,
    elf: Path | str | None = None,
    map_file: Path | str | None = None,
    meta: Path | str | None = None,
    project: str | None = None,
    version: str | None = None,
    branch: str | None = None,
    tags: list | None = None,
    repo: Path | str = ".",
    output: Path | str | None = None,
    dry_run: bool = False,
) -> int | None:
    """Analyse one build and record it. Returns the build id, or None if not recorded.

    The library entry point, for build systems that loop over variants in
    Python: calling this once per variant records each one, where a single
    invocation from the shell would only ever see the last.
    """
    # Loaded here rather than only in main(): a build system calling this
    # directly should not have to know that configuration lives in .env.
    # usecwd, because once installed this file sits in the package directory
    # rather than in the project being built.
    load_dotenv(find_dotenv(usecwd=True))

    config_path = find_config(Path(config) if config else None)
    settings = load_config(config_path)

    section = settings.get("analyse", {})
    elf_path = Path(elf) if elf else Path(section.get("elf", DEFAULT_ELF))
    map_path = Path(map_file) if map_file else Path(section.get("map", DEFAULT_MAP))
    output = output or section.get("output")

    missing = [str(p) for p in (elf_path, map_path) if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Not found: {', '.join(missing)}. "
            f"Point at them with elf=/map_file= or [analyse] in {config_path}"
        )

    analysis = analyse(elf_path, map_path)

    if output:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(analysis, indent=4) + "\n", encoding="utf-8")
        logger.info(f"Analysis written to {output}")

    if os.getenv(ENABLE_ENV, "0").lower() not in TRUTHY:
        logger.info(f"{ENABLE_ENV} is not set, not recording this build")
        return None

    build = build_record(
        load_meta(settings, Path(meta) if meta else None),
        settings,
        tags or [],
        analysis["toolchain"],
        Path(repo),
        project,
        version,
        branch,
    )
    regions = region_records(analysis["regions"], settings)

    if dry_run:
        print(json.dumps({"build": build, "regions": regions}, indent=2, default=str))
        logger.info(f"Dry run: {len(regions)} regions not written")
        return None

    return db.record(build, regions)


def main():
    setup_logging()
    args = parse_args()

    try:
        run(
            config=args.config,
            elf=args.elf,
            map_file=args.map,
            meta=args.meta,
            project=args.project,
            version=args.version,
            branch=args.branch,
            tags=args.tag,
            repo=args.repo,
            output=args.output,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as e:
        logger.error(e)
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to record the build: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
