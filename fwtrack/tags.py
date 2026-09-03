"""Maintenance of what a project has already recorded.

Projects change what they measure. A dimension turns out to duplicate another,
or to have been named badly, or a build gets recorded that should not have been.
Until now that meant writing the SQL by hand.

Talks to the database directly, so it runs where the database is reachable --
on the server, through the container that already holds its credentials:

    fwtrack-tags list --project blinky              dimensions
    fwtrack-tags list --project blinky adeq         values of one of them
    fwtrack-tags drop --project blinky bsp
    fwtrack-tags rename --project blinky cfg config
    fwtrack-tags rename-value --project blinky adeq 52362d NB_B100.EXTLOCK
    fwtrack-tags drop-build 1090

On a server these are reached through ./fwtrack.sh, which runs them in the
container that already holds the database credentials.

Deliberately not reachable over the ingest endpoint. An operation this rare does
not need a route on the open internet, nor a second token on every deployment,
and needing server access before erasing history is a fair barrier. What matters
is here rather than in the transport: the project scope, the dry run, and the
refusal to collapse two builds into one.
"""

import argparse
import sys

from dotenv import find_dotenv, load_dotenv
from tabulate import tabulate

from . import db
from .log import get_logger, setup_logging

logger = get_logger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Edit dimensions and builds already recorded")
    commands = parser.add_subparsers(dest="command", required=True)

    # On the subcommands rather than on the top level: `fwtrack-tags drop x -n`
    # is how anyone would write it, and a flag that only works before the
    # subcommand is a flag that eventually gets missed.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-n", "--dry-run", action="store_true", help="Report what would change and stop"
    )

    listing = commands.add_parser(
        "list", help="Dimensions of a project, or the values of one of them"
    )
    listing.add_argument("--project", required=True)
    listing.add_argument(
        "tag", nargs="?", help="Show this dimension's values instead (a tag, or a field)"
    )

    drop = commands.add_parser(
        "drop", parents=[common], help="Remove a dimension from a project's history"
    )
    drop.add_argument("--project", required=True)
    drop.add_argument("tag")

    rename = commands.add_parser(
        "rename", parents=[common], help="Rename a dimension, keeping its values"
    )
    rename.add_argument("--project", required=True)
    rename.add_argument("old")
    rename.add_argument("new")

    value = commands.add_parser(
        "rename-value", parents=[common], help="Rewrite one value of a dimension"
    )
    value.add_argument("--project", required=True)
    value.add_argument("tag", help="A tag, or one of: branch, origin, author, version, toolchain")
    value.add_argument("old")
    value.add_argument("new")

    build = commands.add_parser(
        "drop-build", parents=[common], help="Delete one build and its regions"
    )
    build.add_argument("build_id", type=int)

    return parser.parse_args()


def print_tags(tags: list, fields: list, project: str) -> None:
    """Everything a project can be filtered or renamed by, in one table.

    Tags and fields are listed together because that is what they are from a
    dashboard. The kind still matters for what can be done to them, hence the
    column: a field's values can be rewritten, but the field itself stays.

    The number of distinct values is the one to read: a handful means a variant
    worth filtering by, hundreds mean something like a commit hash that should
    never have been a dimension.
    """
    rows = [
        [r["tag"], kind, r["builds"], r["values"]]
        for kind, group in (("tag", tags), ("field", fields))
        for r in group
    ]
    if not rows:
        print(f"Nothing recorded for '{project}'")
        return

    print(tabulate(rows, headers=["Dimension", "Kind", "Builds", "Values"], tablefmt="simple"))


def print_values(rows: list, project: str, tag: str) -> None:
    if not rows:
        print(f"'{project}' records no dimension called '{tag}'")
        return

    print(
        tabulate(
            [[r["value"], r["builds"]] for r in rows],
            headers=[tag, "Builds"],
            tablefmt="simple",
        )
    )


def report(dry_run: bool, message: str) -> None:
    print(f"Dry run: {message[0].lower() + message[1:]} (nothing written)" if dry_run else message)


def run(args) -> None:
    with db.connect() as conn:
        if args.command == "list":
            if args.tag:
                print_values(db.tag_values(conn, args.project, args.tag), args.project, args.tag)
            else:
                print_tags(
                    db.tag_counts(conn, args.project),
                    db.field_counts(conn, args.project),
                    args.project,
                )
            return

        if args.command == "drop":
            affected = db.drop_tag(conn, args.project, args.tag, args.dry_run)
            report(args.dry_run, f"'{args.tag}' removed from {affected} builds of '{args.project}'")
            return

        if args.command == "rename":
            affected = db.rename_tag(conn, args.project, args.old, args.new, args.dry_run)
            report(args.dry_run, f"'{args.old}' renamed to '{args.new}' in {affected} builds")
            return

        if args.command == "rename-value":
            affected = db.rename_value(
                conn, args.project, args.tag, args.old, args.new, args.dry_run
            )
            report(
                args.dry_run,
                f"'{args.tag}={args.old}' renamed to '{args.new}' in {affected} builds",
            )
            return

        deleted = db.delete_build(conn, args.build_id, args.dry_run)
        if deleted is None:
            logger.error(f"No build with id {args.build_id}")
            sys.exit(1)

        report(
            args.dry_run, f"build {deleted['id']} deleted: {deleted['project']} {deleted['commit']}"
        )


def main():
    setup_logging()
    args = parse_args()
    load_dotenv(find_dotenv(usecwd=True))

    try:
        run(args)
    except Exception as e:
        logger.error(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
