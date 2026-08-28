# fw-footprint-tracker

Track embedded firmware memory footprint across builds, and see per-branch
trends in Grafana. Answers *where did the memory go?* before the linker does it
for you.

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
already prints.

## Server

```bash
git clone https://github.com/katbert-92/fw-footprint-tracker
cd fw-footprint-tracker && ./fwtrack.sh up
```

Generates secrets, picks free ports, prints the endpoint and token. Later:
`./fwtrack.sh update` (pulls and restarts; `.env` is gitignored and survives). It
follows whatever ref is checked out — see [Versions](#versions).

`./fwtrack.sh` is the only thing to run on the server; `./fwtrack.sh help` lists
what it does.

Ports bind to `127.0.0.1` for a host with a reverse proxy. `BIND_ADDRESS=0.0.0.0`
reaches them directly instead.

| Container | |
|---|---|
| `postgres` | the data; stays on loopback |
| `ingest` | one authenticated POST, so runners never hold database credentials |
| `grafana` | dashboards, generated rather than drawn |

## Project

```bash
pip install git+https://github.com/katbert-92/fw-footprint-tracker@v0.1.0
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
table, so a plain local build stays offline.

Once there is data: `fwtrack-dash --project blinky`.

### Settings

Read from the environment; `.env` in the project root is a convenience, not a
requirement. Every setting can also be passed as an argument.

| | |
|---|---|
| `FWTRACK_ENABLE` | `1` to record; anything else only prints |
| `FWTRACK_URL` + `FWTRACK_INGEST_TOKEN` | send over HTTP |
| `FWTRACK_DSN` | or write straight to the database |
| `FWTRACK_ORIGIN` | label for this run: `merge-request`, `nightly`, `release` |
| `FWTRACK_TAGS` | `board=nucleo,build_type=Release` |

Precedence: arguments, then environment, then `.env`.

### Many variants per run

`fwtrack` records one build. A build system looping over variants in Python
should call the library from inside the loop — a shell step afterwards only
sees the last one.

```python
from fwtrack import track_build

track_build(config="build/fw_tracking.toml", tags=["cfg=2", "board=rev-c"])
```

Nothing is read from the environment that you do not want: pass `url=`,
`token=`, `dsn=`, `read_dotenv=False`.

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

`fwtrack-dash` asks the database which dimensions, areas and regions a project
has. Nothing about a project is hardcoded.

One dashboard per project, in its own folder — folders are where permissions are
granted, and a dashboard variable is a filter, not a boundary. Generated
dashboards are read-only: regenerating rewrites the file. **Save As** for a
private copy; port anything worth keeping back into the generator.

| Panel | |
|---|---|
| Last build delta | what the newest build cost, per region |
| Region usage | how full each region is, against its thresholds |
| Builds | date, commit, branch, author, version, region, size |
| Usage over time | stacked bytes per region, with a capacity line |
| By build | bytes per region per build |
| Delta vs previous | change against the previous build **on the same branch** |

Every time panel is marked where the compiler changed — the answer to "all
regions grew at once, did we change toolchain?" belongs on the chart the jump is
seen on, not in a table. The mark appears only where the value actually changed,
so a toolchain that holds for a year draws one line rather than a thousand.

The build list carries an **Uncommitted** column instead: that one is a property
of a single measurement, not a moment in time. A build made with uncommitted
changes does not correspond to any commit, and its point is placed at the build
time rather than the commit time so that local iterations stay separate.

Variable lists follow the dashboard time range, so a project with thousands of
dead branches stays usable.

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

## Operations

| | |
|---|---|
| `fwtrack` | analyse and record one build |
| `fwtrack-analyse` | analyse only, write JSON |
| `fwtrack-push` | record an analysis produced earlier |
| `fwtrack-dash` | generate a project dashboard |
| `fwtrack-init` | check services, schema and data |
| `fwtrack-server` | the ingest endpoint |
| `fwtrack-tags` | edit dimensions and builds already recorded |

The first three run in the project being measured. The rest need the database,
so on a server they are reached through `./fwtrack.sh`, which runs them in the
container that already holds its credentials.

### Maintenance

Projects change what they measure: a dimension turns out to duplicate another,
or to have been named badly, or a build gets recorded that should not have been.

The script changes to its own directory before doing anything, so it can be
called by its full path — `/opt/fwtrack/fwtrack.sh backup` — from anywhere, and
`cd` is never needed:

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

`./fwtrack.sh check --project blinky` when a dashboard looks empty: it separates
"nothing was recorded" from "the filters exclude everything", which look
identical from the dashboard.

### Data model

```text
builds          per build: project, time, commit, branch, author, version,
                origin, dirty, toolchain, dimensions in a JSONB column
memory_usage    per region of a build: used, total (null if unknown)
region_budgets  warning levels, per project and region
memory_points   a view joining them, with free and percentage computed
```

Dimensions live in JSONB, so adding one needs no migration. Writes upsert on
`(project, commit, built_at, tags)`: re-running a pipeline on the same commit
refreshes the numbers. On a dirty tree the build time is used instead of the
commit time, so local iterations stay separate.

`memory_points` is the stable surface for custom panels:

```sql
SELECT built_at, region, used, pcnt FROM memory_points
WHERE project = 'blinky' AND area = 'flash' ORDER BY built_at DESC;
```

### Changing the set of dimensions

Projects change what they measure. To stop filtering by one without touching
history:

```bash
fwtrack-dash --project blinky --exclude-tags bsp
```

To remove it from the data — currently SQL, an administrator's job rather than
CI's:

```sql
UPDATE builds SET tags = tags - 'bsp' WHERE tags ? 'bsp';
```

### Schema changes

`deploy/schema.sql` runs once, on an empty data directory. Apply an `ALTER` by
hand to an existing database and keep the file in step. A view whose columns
changed needs `DROP VIEW` — `CREATE OR REPLACE` cannot reorder them.

### Backups

```bash
./fwtrack.sh backup
```

## Versions

Releases are tagged on `main`; `dev` is where work lands. Pin both sides to the
same tag: a branch moves under you, a tag does not.

| | |
|---|---|
| project | `pip install git+https://github.com/katbert-92/fw-footprint-tracker@v0.1.0` |
| server | `git checkout v0.1.0 && ./fwtrack.sh up` |

`curl http://<host>:8099/ingest/health` reports the version a server is running,
which is the one thing ssh would otherwise be needed for.

Cutting a release: merge `dev` into `main`, then `make release VERSION=0.1.1`
and push what it prints.

## Requirements

Python 3.11+, a GNU toolchain that emits a linker map, Docker for the server.
No region name is ever hardcoded: a project whose flash is called `ROM` works
like one calling it `FLASH`.

## Licence

GPL-3.0. Running it in CI imposes nothing on the firmware being measured.
