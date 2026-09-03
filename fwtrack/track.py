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
from datetime import UTC, datetime
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
    parser.add_argument(
        "-m", "--meta", type=Path, help="Build metadata JSON, if the project has one"
    )
    parser.add_argument(
        "-c", "--config", type=Path, default=DEFAULT_CONFIG_FILE, help="Tracking config"
    )
    parser.add_argument(
        "--toolchain", default="", help="Override the toolchain recorded by fwtrack-analyse"
    )
    parser.add_argument(
        "-C",
        "--repo",
        type=Path,
        default=Path("."),
        help="Repository to read commit and dirty state from (default: current directory)",
    )
    parser.add_argument(
        "-t",
        "--tag",
        action="append",
        default=[],
        metavar="KEY=VALUE",
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
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"Error reading {path}: {e}")
        sys.exit(1)


def git_output(repo: Path, *args: str, timeout: int = 10) -> str:
    """Run git against an explicit repository.

    Never relies on the current directory: the tool is installed as a package
    and may well be invoked from somewhere other than the project being
    measured, in which case picking up the wrong repository's commit would go
    unnoticed.

    The timeout is generous enough for anything local; the one command here
    that talks to a remote asks for more.
    """
    cmd = ["git", "-C", str(repo), *args]
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=timeout
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning(f"git command {' '.join(cmd)} failed: {e}")
        return ""


# Set by the CI system, in the order they should be trusted. A merge request
# build should be attributed to the branch being merged, not to the temporary
# ref the runner checked out.
CI_BRANCH_VARS = [
    "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME",  # GitLab, merge request pipelines
    "CI_COMMIT_BRANCH",  # GitLab, branch pipelines
    "CI_COMMIT_REF_NAME",  # GitLab, tags included
    "GITHUB_HEAD_REF",  # GitHub, pull requests
    "GITHUB_REF_NAME",  # GitHub, pushes
    "BRANCH_NAME",  # Jenkins
]
DETACHED = "HEAD"

# The subset of CI_BRANCH_VARS that holds the tag on a tag pipeline. Skipped
# when one is detected: a tag is minted per push, so recording one as a branch
# adds a branch that exists once and never again -- and takes the build with
# it, out of the history of the branch it was actually cut from.
REF_NAME_VARS = frozenset({"CI_COMMIT_REF_NAME", "GITHUB_REF_NAME"})

# Consulted in this order when a tagged build has to be attributed to a branch.
# A long-lived branch is a better answer than whichever feature branch happens
# to come first alphabetically.
PREFERRED_BRANCHES = ("main", "master", "dev", "develop")


# Brings the branch tips into a clone that has only the ref which started the
# pipeline. Forced, because the tips move and a stale one would be worse than
# none: it would name a branch this commit is no longer on.
BRANCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"


def branches_containing_head(repo: Path) -> list:
    listed = git_output(repo, "branch", "--all", "--contains", "HEAD", "--format=%(refname:short)")

    names = []
    for line in listed.splitlines():
        name = line.strip().removeprefix("origin/")
        # Skipped: origin/HEAD is a pointer to the default branch rather than a
        # branch, and on a detached HEAD git lists a placeholder of its own --
        # "(HEAD detached at v1.2)", "(no branch)".
        if not name or name == DETACHED or name.startswith(("HEAD", "(")):
            continue

        names.append(name)

    return names


def fetch_branch_tips(repo: Path) -> None:
    """Ask the remote for the branches, since the clone was not given any.

    A CI runner fetches only the ref that started the pipeline, so on a tag
    pipeline there is nothing to search: the tag is there and the branches are
    not. Depth does not help -- a full history of one ref is still one ref.

    Done here rather than asked of every project's CI configuration. A line in
    each .gitlab-ci.yml would work and would also have to be remembered by
    every repository that ever installs this, which is how a tool acquires a
    setup step nobody performs. The credentials are already in the remote the
    runner just cloned from, so there is nothing to configure.
    """
    remotes = git_output(repo, "remote").split()
    if not remotes:
        return

    remote = "origin" if "origin" in remotes else remotes[0]
    logger.info(f"No branch contains HEAD; asking {remote} for the branch tips")
    git_output(repo, "fetch", "--quiet", remote, BRANCH_REFSPEC, timeout=120)


def branch_containing(repo: Path) -> str:
    """The branch a tagged build was cut from, as far as git can tell.

    A tag pipeline has no branch of its own: GitLab leaves CI_COMMIT_BRANCH
    empty and puts the tag in CI_COMMIT_REF_NAME. Recording that as the branch
    adds one dead entry to the dashboard filter per tag -- and a project that
    tags a few times a day drowns the list in a week. The branch containing the
    commit is both stable and more useful: it keeps a tagged build on the same
    line as the work leading up to it, instead of starting a series of one.

    Returns nothing on a clone too shallow to connect the commit to any tip,
    and the caller then records that it could not tell.
    """
    names = branches_containing_head(repo)
    if not names:
        fetch_branch_tips(repo)
        names = branches_containing_head(repo)

    for preferred in PREFERRED_BRANCHES:
        if preferred in names:
            return preferred

    return names[0] if names else ""


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

    tagged = bool(os.getenv("CI_COMMIT_TAG") or os.getenv("GITHUB_REF_TYPE") == "tag")
    if tagged:
        contained = branch_containing(repo)
        if contained:
            logger.info(f"Tagged build: attributing it to the branch '{contained}'")
            return contained

    for name in CI_BRANCH_VARS:
        if tagged and name in REF_NAME_VARS:
            continue

        value = os.getenv(name)
        if value:
            logger.info(f"Detached HEAD: taking the branch from {name}")
            return value

    # An unknown CI, or one that exports no branch of its own. The same
    # question as above, and worth asking once: a tagged build already did.
    contained = "" if tagged else branch_containing(repo)
    if contained:
        logger.info(f"Detached HEAD: git puts the commit on '{contained}'")
        return contained

    # One bad value rather than a new one per push. This is what is left when
    # neither git nor the environment can name a branch, and it has to stay
    # obvious enough to fix rather than quietly grow the branch list.
    logger.warning(
        f"{'Tagged build' if tagged else 'Detached HEAD'} that no branch could be found for; "
        f"recording the branch as '{DETACHED}'. Pass --branch to name it explicitly"
    )
    return DETACHED


ORIGIN_ENV = "FWTRACK_ORIGIN"


def resolve_origin(override: str | None = None) -> str:
    """What kind of run produced this build.

    Defaults to telling a build server apart from someone's machine, which is
    the distinction that always exists. A pipeline that wants finer labels --
    merge request against nightly against release -- sets FWTRACK_ORIGIN, and
    they become values of the dashboard filter without any change here.
    """
    return override or os.getenv(ORIGIN_ENV) or ("ci" if os.getenv("CI") else "local")


def resolve_author(repo: Path, override: str | None = None) -> str | None:
    """Who wrote the commit this build came from.

    A hint, not an attribution: on a branch with many commits this is whoever
    touched it last, and on a merge it is whoever merged. Enough to know who to
    ask about a jump; pinning a jump on a change needs the symbol level.
    """
    if override:
        return override

    return git_output(repo, "log", "-1", "--format=%an") or None


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
            return datetime.fromtimestamp(int(commit_ts), UTC)
        logger.warning("Could not read commit timestamp, falling back to the current time")

    return datetime.now(UTC)


def build_record(
    meta: dict,
    config: dict,
    cli_tags: list,
    toolchain: str,
    repo: Path,
    project_override: str | None = None,
    version_override: str | None = None,
    branch_override: str | None = None,
    author_override: str | None = None,
    origin_override: str | None = None,
) -> dict:
    """Everything about a build except the numbers.

    Only the tags come from the project's metadata file; the rest is read from
    git, so a project that writes no such file works just as well.
    """
    project = project_override or resolve_project(meta, config)
    if not project:
        raise ValueError("No project name: set `project` in the config or pass --project")

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
        "origin": resolve_origin(origin_override),
        "dirty": dirty,
        "author": resolve_author(repo, author_override),
        "toolchain": toolchain or None,
        "tags": collect_custom_tags(meta, config, cli_tags),
    }


def region_records(usage: dict, config: dict) -> list:
    records = []
    for region, info in usage.items():
        area = resolve_area(region, config)
        records.append(
            {
                "region": region,
                "area": area,
                "used": int(info["used"]),
                "total": int(info["total"]),
                "thresholds": resolve_thresholds(region, area, config),
            }
        )

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
