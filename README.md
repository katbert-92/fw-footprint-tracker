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
`./fwtrack.sh update` — pulls, restarts, and regenerates every project's
dashboards, which is what makes it different from `up`. `.env` is gitignored and
survives. It follows whatever ref is checked out — see [Versions](#versions).

Regenerating replaces dashboards edited in Grafana, so `up` never does it on its
own: restarting the stack is not a reason to lose someone's layout.

`./fwtrack.sh` is the only thing to run on the server; `./fwtrack.sh help` lists
what it does.

Ports bind to `127.0.0.1` for a host with a reverse proxy. `BIND_ADDRESS=0.0.0.0`
reaches them directly instead.

| Container | |
| --- | --- |
| `postgres` | the data; stays on loopback |
| `ingest` | one authenticated POST, so runners never hold database credentials |
| `grafana` | dashboards, generated rather than drawn |

### API

Two routes, both under `/ingest/`. `fwtrack` calls the second one for you; it is
documented because a build system that would rather post JSON than install a
Python package can do exactly that.

```bash
curl https://fwtrack.example.com/ingest/health
# {"status": "ok", "version": "0.1.0"}
```

No token, so it can be a health check. The version is the one the server is
actually running, which is otherwise an ssh session away.

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
region from then on.

Writes upsert on `(project, commit, built_at, tags)`, so re-running a pipeline
on the same commit refreshes the numbers instead of adding a second point.

| | |
| --- | --- |
| 201 | recorded |
| 400 | a required field is missing, or `built_at` is not a date |
| 401 | wrong token, or none |
| 413 | body over 1 MB |
| 500 | the write failed; the container log has the reason |

Editing what was recorded is not a route. Deleting a dimension is a rare,
irreversible operation, and a token that every build runner holds should not be
able to do it — so that lives in [Maintenance](#maintenance), behind server
access.

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
| --- | --- |
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
| --- | --- |
| Region usage | how full each region is at its last build, against its thresholds |
| Usage over time | bytes per region and branch, with a capacity line |
| By build | bytes per region per build |
| Delta vs previous | change against the previous build **of the same branch and variant** |

The gauges are the front page; the history sits in a collapsed row below them,
because the first question is "is anything running out", and only the answer
"yes" leads to the second.

Deltas are computed over the whole history and filtered by the dashboard's time
range afterwards, so a build still shows what it cost when the build before it
falls outside the window — which is most of them, since a branch and variant
usually build once a day.

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

### Build activity

`fwtrack-dash` writes a second dashboard alongside the first, about the state of
the project rather than the detail of one region.

| Panel | |
| --- | --- |
| Common | builds, commits, branches and authors in the range |
| Memory areas | how full each area is as a whole, on the latest build |
| Builds over time | how many land, bucketed to the range |
| Latest builds | one row per build: commit, author, and what it did to each area |
| When builds happen | weekday against hour, coloured; its own 30-day window |
| Who builds / branches / origins | where the builds come from |
| How full, per area | every measurement, percent left axis, bytes right |
| Tightest regions | what to worry about, worst first |

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
| --- | --- |
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
