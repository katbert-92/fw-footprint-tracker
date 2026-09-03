#!/usr/bin/env bash
#
# Everything done on the server, in one place.
#
#   ./fwtrack.sh up                                start it, or restart it
#   ./fwtrack.sh update                            pull, restart, regenerate dashboards
#   ./fwtrack.sh backup                            dump the database, gzipped, here
#   ./fwtrack.sh tags list --project blinky        dimensions, and how many builds use each
#   ./fwtrack.sh tags list --project blinky adeq   values of one dimension
#   ./fwtrack.sh tags drop --project blinky bsp    remove a dimension (-n first, to rehearse)
#   ./fwtrack.sh tags rename --project blinky a b  rename a dimension, keeping its values
#   ./fwtrack.sh tags rename-value --project blinky adeq 52362d NB_B100.EXTLOCK
#   ./fwtrack.sh tags drop-build 1090              delete one build and its regions
#   ./fwtrack.sh dash --project blinky             regenerate its dashboard
#   ./fwtrack.sh dash --project blinky --variant-tags tag,platform,type,adeq
#                                                  ... and set the filter order
#   ./fwtrack.sh check                             services, schema, data
#   ./fwtrack.sh logs | down | restart | token
#
# A dispatcher and nothing more. Lifecycle goes to make, data commands go to the
# package inside the ingest container -- which already holds the database
# credentials, so the host needs nothing installed. Anything that needs real
# logic belongs in the package, where it can be tested.

set -euo pipefail
cd "$(dirname "$0")"

# Anchored on the text rather than on line numbers, which move.
usage() {
    sed -n '/^# Everything done/,/^#   \.\/fwtrack\.sh logs/p' "$0" | sed 's/^# \{0,1\}//'
}

in_container() {
    local binary=$1; shift

    if [ -t 0 ]; then
        exec docker compose exec ingest "$binary" "$@"
    fi

    # No terminal: -T so `ssh host './fwtrack.sh tags list ...'` works, and
    # stdin closed because `docker compose exec` otherwise reads it -- which
    # eats the rest of the script in `ssh host bash -s < script`.
    exec docker compose exec -T ingest "$binary" "$@" </dev/null
}

command=${1:-help}
shift || true

case "$command" in
    up|down|restart|logs|ps|token|env)
        exec make "$command" "$@"
        ;;
    update)
        make update "$@"
        # Dashboards too, because "update" means "match what the code now
        # generates". Deliberately not part of `up`: this overwrites anything
        # edited in Grafana, so it belongs to a command that says so.
        in_container fwtrack-dash --out-dir /dashboards --all
        ;;
    backup)
        file="fwtrack-$(date +%F-%H%M).sql.gz"
        # Credentials come from the container's own environment rather than
        # from parsing .env here.
        docker compose exec -T postgres \
            sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' </dev/null | gzip > "$file"
        echo "Written $file ($(du -h "$file" | cut -f1))"
        ;;
    tags)
        in_container fwtrack-tags "$@"
        ;;
    dash)
        # Straight into the directory Grafana provisions from, which is mounted
        # into this container. A --out-dir given by the caller wins over it.
        in_container fwtrack-dash --out-dir /dashboards "$@"
        ;;
    check)
        in_container fwtrack-init "$@"
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        echo "Unknown command: $command" >&2
        echo >&2
        usage >&2
        exit 1
        ;;
esac
