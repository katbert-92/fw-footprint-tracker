# fw-footprint-tracker

Track embedded firmware memory footprint across builds, and see per-branch
trends in Grafana. Answers *where did the memory go?* before the linker does it
for you — and, down to the function, *which change took it*.

```text
   your build                      your server
┌──────────────┐            ┌──────────────────────┐
│ fw.elf       │──fwtrack──▶│  ingest ─┐           │
│ fw.map       │  + token   │          ├▶ Postgres │
└──────────────┘            │  Grafana ┘           │
                            └──────────────────────┘
```

Region sizes come from the MAP file, the layout from ELF program headers, and
usage is measured the way the linker measures it — the same numbers your build
already prints. On ESP-IDF, where the linker's regions do not describe the chip,
it is measured the way the ESP-IDF build checks its image instead.

## Server

```bash
git clone https://github.com/katbert-92/fw-footprint-tracker
cd fw-footprint-tracker && ./fwtrack.sh up
```

Needs Docker, git and python3, and says how to install whichever is missing.
Generates secrets, picks free ports, prints the addresses and the token. `.env`
holds them; it is gitignored and survives updates.

| | |
| --- | --- |
| `./fwtrack.sh up` | start, or restart after a change to `.env` |
| `./fwtrack.sh update` | newer version, restart, regenerate every dashboard |
| `./fwtrack.sh info` | addresses, admin password, token |
| `./fwtrack.sh https DOMAIN` | serve it over HTTPS on that domain |
| `./fwtrack.sh backup` | dump the database into `backups/` |
| `./fwtrack.sh restore FILE` | replace the database with a dump |
| `./fwtrack.sh check` | services, schema, data |
| `./fwtrack.sh help` | the rest |

`update` regenerates dashboards, which replaces any edited in Grafana; `up`
never does, since restarting the stack is not a reason to lose someone's
layout. The script changes to its own directory first, so
`/opt/fwtrack/fwtrack.sh backup` works from anywhere.

### Reaching it

Every port is bound to `127.0.0.1`: reachable from the server itself and from
nowhere else. Builds elsewhere need an address, and the ingest token should not
cross a network in clear text, so something has to accept HTTPS and hand
requests on — `/ingest/` to the ingest service, the rest to Grafana. That is a
reverse proxy: nginx, Caddy, a Cloudflare tunnel. Three ways to have one.

**No web server on the machine yet.** One command, given a domain whose DNS
points at it:

```bash
./fwtrack.sh https fwtrack.example.com
```

Starts Caddy in front of both, with a Let's Encrypt certificate it renews on its
own. Needs ports 80 and 443 free; `./fwtrack.sh https off` undoes it.

No domain at all: `./fwtrack.sh https 203.0.113.5.sslip.io`, with the server's
own IP. [sslip.io](https://sslip.io) resolves such a name to the IP inside it,
which is enough for a certificate.

**A web server already there.** Point it at the two loopback ports. For nginx:

```nginx
location /ingest/ { proxy_pass http://127.0.0.1:8099; }
location /        { proxy_pass http://127.0.0.1:3000; proxy_set_header Host $host; }
```

and set `GRAFANA_ROOT_URL=https://your.domain/` in `.env`, then `./fwtrack.sh up`,
so the links Grafana builds point at the right place. The ports are the ones
`./fwtrack.sh info` prints.

**A trusted network.** `BIND_ADDRESS=0.0.0.0` in `.env` reaches the ports
directly, without TLS.

| Container | |
| --- | --- |
| `postgres` | the data; stays on loopback |
| `ingest` | one authenticated POST, so runners never hold database credentials; brings the schema up to date on every start |
| `grafana` | dashboards, generated rather than drawn |
| `caddy` | HTTPS, only after `./fwtrack.sh https` |

### Who can do what

Grafana opens without a login: anyone who can reach it can look, nobody can
edit. Looking is broader than it sounds — every Grafana viewer can send the
datasource arbitrary SQL — so Grafana connects as `fwtrack_grafana`, an account
that can only read, for at most 30 seconds per query. `GRAFANA_ANONYMOUS=false`
in `.env` puts a login in front, for dashboards that are not for everyone who
can reach the server.

The ingest token writes builds and nothing else. Deleting and renaming need
server access: [Maintenance](#maintenance).

### API

Two routes, both under `/ingest/`. `fwtrack` calls the second one for you; it is
documented because a build system that would rather post JSON than install a
Python package can do exactly that.

```bash
curl https://fwtrack.example.com/ingest/health
# {"status": "ok", "version": "0.5.0"}
```

No token, so it can be a health check, and a 503 when the database is
unreachable. The version is the one the server is actually running, which is
otherwise an ssh session away.

```bash
curl -X POST https://fwtrack.example.com/ingest/builds \
  -H "Authorization: Bearer $FWTRACK_INGEST_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "build": {
      "project": "blinky",
      "built_at": "2026-08-05T10:00:00+00:00",
      "commit": "deadbee",
      "branch": "dev",
      "origin": "ci",
      "dirty": false,
      "version": "1.2.3",
      "author": "Kat",
      "toolchain": "GCC 15.2.1",
      "tags": {"board": "nucleo", "build_type": "Release"}
    },
    "regions": [
      {"region": "FLASH", "area": "flash", "used": 362144, "total": 524288,
       "thresholds": [85, 90, 99]},
      {"region": "SRAM1", "area": "ram", "used": 41232, "total": 131072}
    ]
  }'
# {"build_id": 1091, "regions": 2}
```

`build` requires `project`, `built_at` (ISO 8601), `commit`, `branch`, `origin`
and `dirty`. `regions` requires `region`, `area` and `used`. Everything else
above is optional: a region without `total` records a size that is not known
rather than a wrong one, and `thresholds` sets the warning levels for that
region from then on. Tag names are letters, digits and underscores — each one
becomes a Grafana variable — and values are stored as text.

Writes upsert on `(project, commit, built_at, tags)`, so re-running a pipeline
on the same commit refreshes the numbers instead of adding a second point.

| | |
| --- | --- |
| 201 | recorded |
| 400 | a field is missing or of the wrong kind, `built_at` is not a date, or a tag name is not usable |
| 401 | wrong token, or none |
| 411 | no `Content-Length` |
| 413 | body over 1 MB |
| 500 | the write failed; the container log has the reason |

Editing what was recorded is not a route. Deleting a dimension is a rare,
irreversible operation, and a token that every build runner holds should not be
able to do it — so that lives in [Maintenance](#maintenance), behind server
access.

## Project

```bash
pip install git+https://github.com/katbert-92/fw-footprint-tracker@v0.5.0
```

`fw_tracking.toml` beside the build system:

```toml
project = "blinky"

[analyse]
elf = "build/zephyr/zephyr.elf"
map = "build/zephyr/zephyr.map"

[[group]]
name = "flash"
match = ["FLASH*"]
thresholds = [85, 90, 99]     # warning levels, percent

[[group]]
name = "ram"
match = ["SRAM*", "RAM*"]
```

Then `fwtrack` after each build. Without `FWTRACK_ENABLE` it only prints the
table, so a plain local build stays offline. `fwtrack --check` exits with
status 2 when a region is past the highest of its thresholds — after recording
the build, so the point is on the chart either way.

Once there is data: `./fwtrack.sh dash --project blinky` on the server.

### Settings

Read from the environment; `.env` in the project root is a convenience, not a
requirement. Every setting can also be passed as an argument.

| | |
| --- | --- |
| `FWTRACK_ENABLE` | `1` to record; anything else only prints |
| `FWTRACK_URL` + `FWTRACK_INGEST_TOKEN` | send over HTTP |
| `FWTRACK_DSN` | or write straight to the database |
| `FWTRACK_ORIGIN` | label for this run: `merge-request`, `nightly`, `release` |
| `FWTRACK_TAGS` | `board=nucleo,build_type=Release` |

Precedence: arguments, then environment, then `.env`.

### Dashboard settings

How the project's dashboards are laid out lives in its own `fw_tracking.toml`,
next to everything else about it, and travels with every build:

```toml
[dashboard]
main_branch = "dev"                       # dashboards open on it; the overview keeps to it
overview = { tag = "prd", type = "app" }  # the variant the overview shows
filters = ["tag", "type", "platform"]     # which dimensions are filters, in this order
```

The server takes them from the next build and regenerates the dashboards; a
setting left out of the section goes back to its default. Without the section,
whatever was set with `./fwtrack.sh dash` stays.

### Many variants per run

`fwtrack` records one build. A build system looping over variants in Python
should call the library from inside the loop — a shell step afterwards only
sees the last one.

```python
from fwtrack import track_build

track_build(config="build/fw_tracking.toml", tags=["cfg=2", "board=rev-c"])
```

Nothing is read from the environment that you do not want: pass `url=`,
`token=`, `dsn=`, `read_dotenv=False`. `check=True` raises `BudgetExceeded`
the way `--check` exits.

For a closer look while debugging, `sections=True` (`fwtrack --sections`) also
prints every allocated section with its region and size.

### Branches in CI

The branch comes from git, and from the CI environment when git cannot name it
— a runner checks out a commit, not a branch.

A tag pipeline defeats both: HEAD is detached, and the runner exports the tag
rather than a branch. Recording the tag would mint a branch per push, each with
one build in it, and take that build out of the history it belongs to. So the
tracker asks the remote which branches contain the commit. The clone has none
to search — a runner fetches only the ref that started the pipeline — so it
fetches the branch tips first, over the remote the runner already cloned from.
Nothing to add to a CI configuration, which is the point: it works the same in
a repository that has never heard of this tool.

Where that fails — an unreachable remote, a clone too shallow to connect the
commit to any tip — the branch is recorded as `HEAD` and said so in the log.
One value to notice and fix, rather than a branch list that grows for ever.
`--branch` names it outright and skips all of the above.

### ESP-IDF

Recognised on its own, by the `flasher_args.json` the build writes. The linker
regions do not describe the chip there: flash is reached through the cache MMU,
so `irom_seg` and `drom_seg` are address windows of up to 32 MiB — on the newer
chips one window under two names. What fills up is the app partition, so that
is what is recorded, as `app`: the `.bin` against the smallest app partition,
the check `idf.py build` itself ends with. RAM regions are measured as
everywhere else; cache windows and fixed reservations are left out.

To run it on every build, after `project()` in the top-level `CMakeLists.txt`:

```cmake
find_program(FWTRACK fwtrack HINTS "$ENV{IDF_PYTHON_ENV_PATH}/bin" NO_CACHE)
if(FWTRACK)
    idf_build_get_property(build_dir BUILD_DIR)
    add_custom_target(footprint ALL
        COMMAND ${FWTRACK} --config ${CMAKE_CURRENT_SOURCE_DIR}/fw_tracking.toml
            --elf ${build_dir}/${CMAKE_PROJECT_NAME}.elf
            --map ${build_dir}/${CMAKE_PROJECT_NAME}.map
            --meta ${build_dir}/project_description.json
        WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
        USES_TERMINAL VERBATIM)
    add_dependencies(footprint gen_project_binary)
endif()
```

`pip install` it in the ESP-IDF environment — the shell where `idf.py` works —
and `idf.py reconfigure`, which is also what picks up installing or removing it
later. Where it is not installed the step is skipped, so the firmware builds the
same for everyone. [examples/esp-idf.toml](examples/esp-idf.toml)
has groups for every chip family.

### In CI

GitLab: record every build, keep a snapshot, and on a merge request show what
it costs against the target branch.

```yaml
firmware:
  variables:
    FWTRACK_ENABLE: "1"   # FWTRACK_URL, FWTRACK_INGEST_TOKEN: CI/CD variables
  script:
    - make all            # idf.py build, west build, ...
    - fwtrack --check
    - fwtrack-size -o footprint.json
    - |
      if [ -n "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME" ]; then
        curl -sfo base.json -H "JOB-TOKEN: $CI_JOB_TOKEN" \
          "$CI_API_V4_URL/projects/$CI_PROJECT_ID/jobs/artifacts/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME/raw/footprint.json?job=$CI_JOB_NAME" &&
        fwtrack-size footprint.json --diff base.json --max-growth 4096
      fi
  artifacts:
    paths: [footprint.json]
```

`fwtrack-size --markdown` prints the same diff as tables, ready to post as a
merge request note.

## Where the bytes went

```bash
fwtrack-size                           # this build: each region by object and symbol
fwtrack-size -o before.json            # keep a snapshot
fwtrack-size --diff before.json        # after a change: what moved, and where
fwtrack-size new.elf --diff old.elf    # two builds, each map beside its ELF
fwtrack-size --tree                    # by directory, each with the total under it
```

```text
app: +304 B
libmain.a(main.c.obj)  +308  2,291 → 2,599
    demo_curve         +128        0 → 128  new
    (strings)           +76      726 → 802
    Main_DemoCurve      +58         0 → 58  new
    app_main            +46      396 → 442
(fill)                   -4      642 → 638
```

The map names the object every input section came from, so no debug info is
needed; DWARF fills in what the map cannot say, such as the source of a
function under LTO. Every level adds up to the region total that gets recorded.
String literals have no symbol and stay with the object that brought them, as
`(strings)`; what the linker saved by folding duplicates across objects is
`(merged)`; whatever no input section covers — alignment, image headers — is
`(other)`.

`--markdown` prints the diff for a merge request, `--max-growth BYTES` fails
the job when any region grew by more, `--top` and `--symbols` set how much is
listed. Snapshots compare across machines: paths inside the project are
relative, toolchain paths are dropped.

## Dimensions

Anything worth filtering by is a tag. Tags become single-choice dashboard
filters, each narrowing the next, so they should identify a **variant** —
optimisation level, board revision, feature set.

```toml
[meta]                          # optional; a file the project already writes
file = "debug/fw_info.json"     # JSON or TOML
project = "prj"                 # key holding the project name
version = "version"             # key holding the version
tags = ["type", "cfg"]          # keys copied across as dimensions
```

Three traps, all of which cost us a debugging session each:

**Derived dimensions.** An optimisation level implied by a config index becomes
a second filter that is easy to set to a combination that never existed. Panels
then go blank with no hint why. Fold it into the thing it derives from —
`platform=m4r0c1_b0`, not `platform` plus `bsp`.

**Dimensions that change every build.** A commit hash as a tag gives a dropdown
with one entry per build. Commit, branch, version and author are recorded as
fields already, and filtered with multiple choice.

**Hashes where names exist.** `NB_B100.EXTLOCK` says what was built; `52362d`
has to be looked up every time it appears.

A build missing a dimension shows as `(none)` rather than disappearing, so
history recorded before a tag existed stays visible.

## Dashboards

Generated from what the database holds — which dimensions, areas and regions a
project has — so nothing about a project is hardcoded. The ingest service does
it on its own: for a project's first build, and whenever a build brings a new
dimension, a new memory area or changed [dashboard settings](#dashboard-settings).
`./fwtrack.sh dash --project blinky` does it by hand.

One dashboard per project, in its own folder — folders are where permissions are
granted, and a dashboard variable is a filter, not a boundary. Generated
dashboards are read-only: regenerating rewrites the file. **Save As** for a
private copy; port anything worth keeping back into the generator.

| Panel | |
| --- | --- |
| Region usage | how full each region is at its last build, against its thresholds |
| Usage over time | bytes per region and branch, with a capacity line |
| By build | bytes per region per build |

The gauges are the front page; the history sits in a collapsed row below them,
because the first question is "is anything running out", and only the answer
"yes" leads to the second.

A build made with uncommitted changes does not correspond to any commit: its
hash carries a star, and its point is placed at the build time rather than the
commit time so that local iterations stay separate.

Variable lists follow the dashboard time range, so a project with thousands of
dead branches stays usable.

### Overview

`fwtrack-dash` writes two dashboards per project. **Regions** is the one above:
every panel on it is per region, which is where a memory problem is eventually
tracked down. **Overview** is the one to open first — the state of the project
rather than the detail of one region.

| Panel | |
| --- | --- |
| Common | builds, commits, branches and authors in the range |
| Memory areas | how full each area is as a whole, on the latest build |
| What each build cost | bytes gained or lost per area, a bar per commit |
| Latest builds | one row per build: commit, author, and what it did to each area |
| When builds happen | weekday against hour, coloured; its own 30-day window |
| Who builds / branches / origins | where the builds come from |
| How full, per area | every measurement, percent left axis, bytes right |
| Tightest regions | what to worry about, worst first |

Deltas compare a build with the previous build **of the same branch and
variant**, and are computed over the whole history before the time range is
applied, so a build still shows what it cost when the build before it falls
outside the window — which is most of them, since a branch and variant usually
build once a day.

Counting panels count commits as well as builds. One push fans out into a build
per variant, so a project with twenty-eight variants shows twenty-eight builds
for a single commit, and a builds column on its own reads as if somebody had
spent the day compiling.

Each title says what it covers -- `· all branches` or the pinned branch -- since
the two kinds of panel sit side by side.

`When builds happen` is the one panel on its own clock. A rhythm needs weeks
before it is a rhythm, while the memory panels beside it want the last few days;
whichever range the picker is on, one of the two would be wrong. It takes a
fixed 30 days and Grafana marks the override in its header.

A dirty tree gets a star on its hash in `Latest builds`: the commit is real, but
checking it out would not give you the firmware that was measured.

`What each build cost` puts the commit on the x axis rather than the time. The
same deltas are in the build list a column at a time; as a shape they answer the
question a chart is opened for, which is *which* commit it was. Builds arrive in
bursts of a dozen and then nothing until the next day, so a calendar axis would
draw a cliff and a desert — here every build gets the same width, and the run of
unchanged builds either side of a jump stays readable as a flat stretch.

Its y axis is symlog — a log scale that stays linear across the origin, because
a delta is regularly zero and often negative and a plain log axis can draw
neither. Most builds move a handful of bytes and one in a hundred moves five
kilobytes; on a linear axis that one bar is the chart and the rest are a flat
line.

It has no y axis. Grafana leaves log tick generation to uPlot and offers nothing
to thin it out, so the labels stack into an unreadable column at any height this
panel is likely to get; a log axis makes a poor ruler in any case. What is left
is a zero line, the value on each bar, and the exact bytes in the tooltip and in
the build list below.

The panels about the flow of work count the whole project. The ones about how
much room is left cannot: a bootloader on one board and an application on
another have different memories, and summing them invents a chip that does not
exist. So they are narrowed — but to a slice the project fixes once, rather than
to dropdowns to be set again on every visit:

```bash
./fwtrack.sh dash --project blinky --overview-pin tag=prd,type=app,branch=dev
```

Remembered with the project. Whatever is left unpinned stays a filter, which is
how a project keeps the one dimension it does want to flip between — usually the
board. Dimensions and the `branch`, `origin`, `version` and `toolchain` columns
can all be pinned.

Pinning a dimension also takes it out of the *other* dropdowns' options: a
feature set that only the bootloader is built with is no longer offered on a
dashboard pinned to the application, where choosing it could only ever show
No data.

Passing nothing keeps what is stored, which is what makes regeneration safe.
What is stored is printed on every generation; to forget it:

```bash
./fwtrack.sh dash --project blinky --reset overview_pins    # or main_branch, variant_tags, all
```

A per-build log is deliberately absent: a project that builds a dozen variants
per commit turns one build into a dozen rows of the same hash.

### Filter order

The dimension filters are sequential. The first offers every value it has; each
one after it offers only the values that occur together with what is already
chosen. Choose `adeq` first and the platform list narrows to the platforms that
feature set was built for — which is backwards if you think in hardware first,
and it is not obvious from the dashboard that this is what happened.

So the widest thing a build belongs to goes first, the most specific last. Set
it once, in the order the filters should cascade:

```bash
./fwtrack.sh dash --project blinky --variant-tags platform,type,tag,adeq
```

Stored with the project, so later regenerations keep it. It decides order only:
a dimension the project stopped recording drops out on its own, and one it
started recording since appears at the end rather than going missing. To leave
one out of the filters entirely, `--exclude-tags`.

Every project also has one branch that matters more than the rest, and opening on
all of them buries it:

```bash
./fwtrack.sh dash --project blinky --main-branch dev
```

Remembered the same way, and used as what the branch filter is set to when the
memory dashboard is opened.

## Operations

| | |
| --- | --- |
| `fwtrack` | analyse and record one build |
| `fwtrack-size` | break a build down by object and symbol, or compare two |
| `fwtrack-analyse` | analyse only, write JSON |
| `fwtrack-push` | record an analysis produced earlier |
| `fwtrack-dash` | generate a project's dashboards |
| `fwtrack-init` | check services, schema and data |
| `fwtrack-server` | the ingest endpoint |
| `fwtrack-tags` | edit dimensions and builds already recorded |

The first four run in the project being measured. The rest need the database,
so on a server they are reached through `./fwtrack.sh`, which runs them in the
container that already holds its credentials.

### Maintenance

Projects change what they measure: a dimension turns out to duplicate another,
or to have been named badly, or a build gets recorded that should not have been.

```bash
./fwtrack.sh backup                                   # first, always

./fwtrack.sh tags list --project blinky               # dimensions, and how many builds use each
./fwtrack.sh tags list --project blinky adeq          # values of one of them

./fwtrack.sh tags drop --project blinky bsp -n        # what it would do
./fwtrack.sh tags drop --project blinky bsp           # do it
./fwtrack.sh tags rename --project blinky cfg config
./fwtrack.sh tags rename-value --project blinky adeq 52362d NB_B100.EXTLOCK
./fwtrack.sh tags drop-build 1090

./fwtrack.sh dash --project blinky                    # regenerate the dashboard afterwards
```

`rename-value` is how history recorded before a project started naming things
is folded in: a build labelled `52362d` and a build labelled `NB_B100.EXTLOCK`
are the same variant, and until they share a value they are two series on every
chart. It works on `branch`, `origin`, `author`, `version` and `toolchain` too,
which are columns rather than tags but the same thing from a dashboard:

```bash
./fwtrack.sh tags list --project blinky origin
./fwtrack.sh tags rename-value --project blinky origin mr merge-request
```

Every edit is scoped to one project. An edit that would leave two builds
identical — same commit, same time, same dimensions — is refused rather than
guessed at; delete the redundant build first.

To stop filtering by a dimension without touching history:
`./fwtrack.sh dash --project blinky --exclude-tags bsp`.

`./fwtrack.sh check --project blinky` when a dashboard looks empty: it separates
"nothing was recorded" from "the filters exclude everything", which look
identical from the dashboard.

### Backups

```bash
./fwtrack.sh backup                                      # backups/fwtrack-2026-10-17-020000.sql.gz
./fwtrack.sh restore backups/fwtrack-2026-10-17-020000.sql.gz
```

`restore` replaces everything in one transaction: it either completes or leaves
the database as it was. Dumps made by older versions restore as well, which is
also how a tracker moves to another server — backup there, copy the file,
restore here.

### Data model

```text
builds            per build: project, time, commit, branch, author, version,
                  origin, dirty, toolchain, dimensions in a JSONB column
memory_usage      per region of a build: used, total (null if unknown)
region_budgets    warning levels, per project and region
memory_points     a view joining them, with free and percentage computed
project_settings  per project: dimension order, main branch, overview pins
```

Dimensions live in JSONB, so adding one needs no migration. Writes upsert on
`(project, commit, built_at, tags)`: re-running a pipeline on the same commit
refreshes the numbers, regions included. On a dirty tree the build time is used
instead of the commit time, so local iterations stay separate.

`memory_points` is the stable surface for custom panels:

```sql
SELECT built_at, region, used, pcnt FROM memory_points
WHERE project = 'blinky' AND area = 'flash' ORDER BY built_at DESC;
```

### Schema changes

[fwtrack/schema.sql](fwtrack/schema.sql) is applied by the ingest service every
time it starts, so an update brings an existing database along on its own. Every
statement in it is safe to repeat: add to it, never rename, and keep new view
columns at the end. Against a database of your own, `fwtrack-init --apply-schema`
does the same.

## Versions

Releases are tagged on `main`; `dev` is where work lands. Pin both sides to the
same tag: a branch moves under you, a tag does not.

| | |
| --- | --- |
| project | `pip install git+https://github.com/katbert-92/fw-footprint-tracker@v0.5.0` |
| server | `./fwtrack.sh update v0.5.0` |

On a server checked out at a release, `./fwtrack.sh update` moves to the newest
one; on a branch, it pulls. `curl https://<host>/ingest/health` reports the
version a server is running.

Cutting a release: merge `dev` into `main`, then `./scripts/release.sh 0.5.1`.
It sets the version in `pyproject.toml`, commits, tags and pushes. A tag made
by hand keeps the old version, and pip will not see the update.

## Development

```bash
pip install -e ".[dev]" && pre-commit install
pytest           # FWTRACK_TEST_DSN=postgresql://... runs the database tests too
```

The fixtures in [tests/fixtures/cortex-m](tests/fixtures/cortex-m) are real
Arm GNU builds; `build.sh` there regenerates them.

## Requirements

Python 3.11+, a GNU toolchain that emits a linker map, Docker for the server.
No region name is ever hardcoded: a project whose flash is called `ROM` works
like one calling it `FLASH`.

## Licence

GPL-3.0. Running it in CI imposes nothing on the firmware being measured.
