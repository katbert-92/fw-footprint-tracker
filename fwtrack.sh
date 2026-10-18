#!/usr/bin/env bash
#
# Everything done on the server, in one place.
#
#   ./fwtrack.sh up                              start it, or restart it after a change
#   ./fwtrack.sh update [VERSION]                newer version, restart, regenerate dashboards
#   ./fwtrack.sh info                            addresses, admin password, ingest token
#   ./fwtrack.sh https fwtrack.example.com       serve it over HTTPS on that domain
#   ./fwtrack.sh https off                       stop doing that
#   ./fwtrack.sh backup [DIR]                    dump the database, gzipped, into backups/
#   ./fwtrack.sh restore FILE [-y]               replace the database with a dump
#   ./fwtrack.sh check [--project P]             services, schema, data
#   ./fwtrack.sh dash --project blinky           regenerate a project's dashboards
#   ./fwtrack.sh dash --project blinky --variant-tags board,type
#                                                ... and set the filter order
#   ./fwtrack.sh tags list --project blinky      dimensions, and how many builds use each
#   ./fwtrack.sh tags list --project blinky cfg  values of one dimension
#   ./fwtrack.sh tags drop --project blinky bsp  remove a dimension (-n first, to rehearse)
#   ./fwtrack.sh tags rename --project blinky a b
#   ./fwtrack.sh tags rename-value --project blinky cfg 52362d NB_B100
#   ./fwtrack.sh tags drop-build 1090            delete one build and its regions
#   ./fwtrack.sh status | logs | down | restart
#
# A dispatcher and nothing more. Lifecycle goes to docker compose, data commands
# to the package inside the ingest container -- which already holds the database
# credentials, so the host needs nothing but Docker, git and python3. Anything
# that needs real logic belongs in the package, where it can be tested.

set -euo pipefail
cd "$(dirname "$0")"

# Anchored on the text rather than on line numbers, which move.
usage() {
    sed -n '/^# Everything done/,/^#   \.\/fwtrack\.sh status/p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
    echo "$*" >&2
    exit 1
}

need_docker() {
    command -v docker >/dev/null ||
        die "Docker is not installed. On a Linux server: curl -fsSL https://get.docker.com | sh"
    docker compose version >/dev/null 2>&1 ||
        die "The Docker Compose plugin is missing: apt install docker-compose-plugin (or reinstall Docker from get.docker.com)"
    docker info >/dev/null 2>&1 ||
        die "Docker does not answer. Is it running, and may this user use it? (root, or the docker group)"
}

env_file() {
    command -v python3 >/dev/null || die "python3 is needed to write .env: apt install python3"
    python3 deploy/make-env.py "$@"
}

# Read one value from .env without sourcing it: a value with a space in it
# would otherwise run as a command.
env_get() {
    [ -f .env ] && sed -n "s/^$1=//p" .env | tail -n 1
}

in_container() {
    local binary=$1
    shift

    need_docker
    docker compose ps --status running --services 2>/dev/null | grep -qx ingest ||
        die "The stack is not running: ./fwtrack.sh up"

    if [ -t 0 ]; then
        exec docker compose exec ingest "$binary" "$@"
    fi

    # No terminal: -T so `ssh host './fwtrack.sh tags list ...'` works, and
    # stdin closed because `docker compose exec` otherwise reads it -- which
    # eats the rest of the script in `ssh host bash -s < script`.
    exec docker compose exec -T ingest "$binary" "$@" </dev/null
}

wait_ingest() {
    for _ in $(seq 60); do
        if docker compose exec -T ingest python -c \
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/ingest/health')" \
            </dev/null >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    die "The ingest service did not come up: ./fwtrack.sh logs ingest"
}

info() {
    local domain bind host grafana ingest
    domain=$(env_get FWTRACK_DOMAIN)
    bind=$(env_get BIND_ADDRESS)

    if [ -n "$domain" ]; then
        grafana="https://$domain"
        ingest="https://$domain"
    else
        host=$([ "$bind" = "0.0.0.0" ] && echo "<this server>" || echo "127.0.0.1")
        grafana="http://$host:$(env_get GRAFANA_PORT)"
        ingest="http://$host:$(env_get INGEST_PORT)"
    fi

    echo
    echo "Grafana   $grafana   (admin / $(env_get GRAFANA_ADMIN_PASSWORD))"
    echo "Ingest    $ingest/ingest/health"
    echo
    echo "In the CI of the project being measured:"
    echo "  FWTRACK_ENABLE=1"
    echo "  FWTRACK_URL=$ingest"
    echo "  FWTRACK_INGEST_TOKEN=$(env_get FWTRACK_INGEST_TOKEN)"

    if [ -z "$domain" ] && [ "$bind" != "0.0.0.0" ]; then
        echo
        echo "Reachable from this machine only. To open it to the network:"
        echo "  ./fwtrack.sh https fwtrack.example.com   HTTPS on a domain pointing here"
        echo "  or point a web server you already run at the two ports above"
    fi
}

up() {
    need_docker
    env_file
    docker compose up -d --build --remove-orphans
    if [ "${1:-}" = "--dashboards" ]; then
        # Part of update, because "update" means "match what the code now
        # generates". Not of up: it overwrites anything edited in Grafana.
        wait_ingest
        docker compose exec -T ingest fwtrack-dash --out-dir /dashboards --all </dev/null
    fi
    info
}

update() {
    command -v git >/dev/null || die "git is needed to update"
    local before
    before=$(git describe --tags --always)

    git fetch --quiet --tags
    if [ -n "${1:-}" ]; then
        git checkout --quiet "$1"
    elif git symbolic-ref -q HEAD >/dev/null; then
        git pull --quiet --ff-only
    else
        # On a release tag rather than a branch: move to the newest release.
        local latest
        latest=$(git tag --list 'v*' --sort=-v:refname | head -n 1)
        [ -n "$latest" ] || die "Neither on a branch nor on a release: ./fwtrack.sh update <branch or tag>"
        git checkout --quiet "$latest"
    fi
    echo "Version: $before -> $(git describe --tags --always)"

    # The rest is done by the script that was just checked out, not by this
    # copy of the old one.
    exec "$BASH" ./fwtrack.sh up --dashboards
}

https_mode() {
    local domain=${1:-}
    [ -n "$domain" ] || die "Usage: ./fwtrack.sh https fwtrack.example.com | off"

    if [ "$domain" = off ]; then
        env_file --set FWTRACK_DOMAIN= COMPOSE_PROFILES= GRAFANA_ROOT_URL=http://localhost:3000/
        docker compose rm --stop --force caddy >/dev/null 2>&1 || true
        up
        return
    fi

    local address
    address=$(python3 -c "import socket,sys; print(socket.gethostbyname(sys.argv[1]))" "$domain" 2>/dev/null) ||
        die "$domain does not resolve. Point its DNS A record at this server first."
    echo "$domain resolves to $address -- that has to be this server for the certificate."

    env_file --set "FWTRACK_DOMAIN=$domain" COMPOSE_PROFILES=https "GRAFANA_ROOT_URL=https://$domain/"
    up
    echo
    echo "The first visit may take a few seconds while the certificate is issued."
}

backup() {
    need_docker
    local dir=${1:-backups}
    mkdir -p "$dir"
    local file
    file="$dir/fwtrack-$(date +%F-%H%M%S).sql.gz"

    # Credentials come from the container's own environment rather than from
    # parsing .env here. --clean so the dump can be restored over a database
    # that exists; no privileges, because the roles they name are recreated by
    # the ingest service rather than carried in the dump.
    if ! docker compose exec -T postgres sh -c \
        'pg_dump --clean --if-exists --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB"' \
        </dev/null | gzip >"$file"; then
        rm -f "$file"
        die "The dump failed; nothing was written"
    fi
    echo "Written $file ($(du -h "$file" | cut -f1))"
}

restore() {
    need_docker
    local file="" yes=""
    for arg in "$@"; do
        case "$arg" in
            -y | --yes) yes=1 ;;
            *) file=$arg ;;
        esac
    done
    [ -f "$file" ] || die "Usage: ./fwtrack.sh restore FILE.sql.gz [-y]"

    if [ -z "$yes" ]; then
        [ -t 0 ] || die "Replacing the database needs a yes: add -y"
        read -r -p "Replace everything in the database with $file? [y/N] " answer
        [ "$answer" = y ] || [ "$answer" = Y ] || die "Nothing changed"
    fi

    local reader=cat
    case "$file" in *.gz) reader="gunzip -c" ;; esac

    # One transaction: the schema is emptied and refilled, or nothing happens
    # at all. That also takes dumps made before backup cleaned up after itself.
    docker compose stop ingest
    if ! {
        echo "SET client_min_messages = warning;"
        echo "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"
        $reader "$file"
    } |
        docker compose exec -T postgres sh -c \
            'psql -q -v ON_ERROR_STOP=1 --single-transaction -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
            >/dev/null; then
        docker compose start ingest
        die "The restore failed and was rolled back; the database is as it was"
    fi

    # Brings the schema and Grafana's account back on top of what was loaded.
    docker compose start ingest
    wait_ingest
    docker compose exec -T ingest fwtrack-dash --out-dir /dashboards --all </dev/null
    echo "Restored $file"
}

command=${1:-help}
shift || true

case "$command" in
    up)
        up "$@"
        ;;
    update)
        update "$@"
        ;;
    info | token)
        info
        ;;
    https)
        https_mode "$@"
        ;;
    backup)
        backup "$@"
        ;;
    restore)
        restore "$@"
        ;;
    status | ps)
        need_docker
        docker compose ps
        ;;
    logs)
        need_docker
        exec docker compose logs -f --tail=100 "$@"
        ;;
    down | restart)
        need_docker
        exec docker compose "$command" "$@"
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
    help | -h | --help)
        usage
        ;;
    *)
        echo "Unknown command: $command" >&2
        echo >&2
        usage >&2
        exit 1
        ;;
esac
