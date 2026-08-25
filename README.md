# fw-footprint-tracker

Track embedded firmware memory footprint (Flash/RAM) across builds, and
visualize per-branch trends in Grafana.

Answers the question every embedded team eventually asks too late: *where did
the memory go?* Each build records how full every memory region is; the
dashboards show the trend over time, the difference against the previous build,
and how far each region is from its limit.

```
   your build                       your server
┌────────────────┐            ┌──────────────────────┐
│ fw.elf         │            │  ingest  ─┐          │
│ fw.map         │──fwtrack──▶│           ├▶ Postgres│
└────────────────┘   HTTPS    │  Grafana ─┘          │
                     + token  └──────────────────────┘
```

## Running the server

```bash
git clone https://github.com/katbert-92/fw-footprint-tracker
cd fw-footprint-tracker && make up
```

That is the whole installation. `make up` generates `.env` with fresh secrets on
first run, builds the images and prints what a project needs:

```
Grafana   http://localhost:3000  (admin / ...)
Ingest    http://localhost:8099
Token     kZ8...
```

Ports bind to `127.0.0.1`, for a host that already has a reverse proxy. Set
`BIND_ADDRESS=0.0.0.0` in `.env` to reach the services directly instead.

The ingest token travels in a header, so anything reachable beyond a trusted
network wants a reverse proxy terminating TLS in front.

### What the three containers are

**postgres** holds the data. It stays on the loopback interface: nothing outside
the host needs to speak to it directly.

**ingest** is a small HTTP endpoint that accepts one build at a time,
authenticated with a bearer token. It exists so that build runners never need
database credentials — a leaked token can write build metrics and nothing else.

**grafana** reads the database and serves the dashboards, which are generated
rather than drawn by hand.

## Adding a project

Install the package where the firmware is built:

```bash
pip install git+https://github.com/katbert-92/fw-footprint-tracker@v0.1.0
```

Put a `fw_tracking.toml` next to the build system:

```toml
project = "blinky"

[analyse]
elf = "build/zephyr/zephyr.elf"
map = "build/zephyr/zephyr.map"
output = "build/fw_sections.json"      # optional

[[group]]
name = "flash"
title = "Internal Flash"
match = ["FLASH*"]

[[group]]
name = "ram"
match = ["SRAM*", "RAM*"]
```

Set three variables — in CI, as masked variables:

```bash
FWTRACK_ENABLE=1
FWTRACK_URL=https://fwtrack.example.com
FWTRACK_INGEST_TOKEN=...
```

Then run `fwtrack` after each build. Without `FWTRACK_ENABLE` it only prints the
table, so a plain local build stays offline and needs no credentials at all.

```
+----------------+------------+--------------+-------------+-----------+
| Region         | Origin     | Total (KB)   | Used (KB)   |  Usage %  |
+================+============+==============+=============+===========+
| FLASH          | 0x08010000 | 512          | 225.80      |  44.10%   |
| SRAM1          | 0x20000000 | 60           | 46.20       |  76.99%   |
+----------------+------------+--------------+-------------+-----------+
```

Finally, generate the dashboard once there is data:

```bash
fwtrack-dash --project blinky
```

### If the build system is Python

`fwtrack` handles one build per invocation, which is a problem when a single run
produces many variants: a shell step after the build only ever sees the last
one. Call the library from inside the loop instead:

```python
from fwtrack import track_build

track_build(config="build/fw_tracking.toml")
```

## Configuration

Everything in `fw_tracking.toml` is specific to the project. The tool sets only
the fields the dashboards are built on — project, region, area, commit, branch,
version, origin, dirty, toolchain — and reads the rest from git.

### Memory areas

Linker regions are grouped into the areas the dashboard repeats over. First
matching group wins, so narrower patterns go first. A region matching nothing is
still recorded, under the area `other`, so a region newly added to a linker
script shows up rather than vanishing.

```toml
[[group]]
name = "flash"
title = "Internal Flash"
match = ["FLASH", "FLASH_GRAPHICS"]     # fnmatch: *, ?, [seq]
thresholds = [85, 90, 99]               # warning levels, percent
```

Thresholds are a list, so each entry adds a colour band: `[85, 90, 99]` renders
green, yellow, orange, red. Resolved most specific first:

```toml
[defaults]
thresholds = [75, 85, 95]

[region.SRAM1]
thresholds = [70, 80, 90]               # beats the group it belongs to
```

The physical size of a region is never configured — it comes from the MAP file.
These are policy on top of it.

### Custom dimensions

Anything else worth slicing by is a tag. Tags become dashboard filters, so they
should be things that identify a *variant* — optimisation level, board revision,
feature set — not things that change every build.

Three sources, each overriding the one above:

```toml
[meta]
file = "debug/fw_info.json"             # JSON or TOML the build already writes
project = "prj"                         # key holding the project name
version = "version"                     # key holding the version
tags = ["type", "cfg", "platform"]      # keys copied across as dimensions
```

```bash
FWTRACK_TAGS="board=nucleo,build_type=Release"
fwtrack --tag board=nucleo
```

`[meta]` is never a format this tool defines: it reads a file the project
already writes, under the key names it already uses. A project without one omits
the section entirely.

Two things to avoid:

**Dimensions derived from one another.** An optimisation level implied by a
config index becomes a second filter that is easy to set to a combination that
never existed, and the panels then go blank.

**Dimensions that change every build.** A commit hash as a tag gives a dropdown
with one entry per build. Commit, branch and version are recorded as fields
already.

## Dashboards

Generated, not drawn. `fwtrack-dash` asks the database which dimensions, areas
and regions a project has, so nothing about a project is hardcoded: one calling
its dimensions `board` and `build_type` works like one calling them `cfg` and
`bsp`.

```bash
fwtrack-dash --project blinky                    # write JSON for provisioning
fwtrack-dash --project blinky --push \
             --folder-uid <uid>                  # upload into a running Grafana
```

The file lands in `deploy/grafana/dashboards/<project>/`, which the provisioner
turns into a Grafana folder — folders being where per-project permissions are
granted. One dashboard per project rather than one with a project variable: a
variable is a filter, not a boundary, and anyone who can open the dashboard can
switch it.

Generated dashboards are read-only in the UI, because regenerating rewrites the
file wholesale. To customise one, **Save As** — the copy lives in Grafana's own
database and is never touched. Anything worth keeping for everyone belongs in
the generator.

### Panels

| Panel | Shows |
|---|---|
| Last build delta | what the newest build cost, per region |
| Region usage | how full each region is now, against its thresholds |
| Builds | date, commit, branch, version, region, size |
| Usage over time | bytes per region, stacked, with a capacity line |
| By build | bytes per region per build |
| Delta vs previous build | change against the previous build on the same branch |

Filters: the build dimensions (single choice, each narrowing the next), plus
branch, build origin, memory area and region (multiple choice). Variable lists
follow the dashboard time range, so a project with thousands of dead branches
stays usable.

A dimension a build does not carry shows as `(none)` rather than hiding the
build. That matters when a tag is added or removed: history recorded before the
change is still visible, under a value that says so.

## Operations

### Commands

| | |
|---|---|
| `fwtrack` | analyse and record one build |
| `fwtrack-analyse` | analyse only, write JSON |
| `fwtrack-push` | record an analysis produced earlier |
| `fwtrack-dash` | generate a project dashboard |
| `fwtrack-init` | check the services, the schema and the data |
| `fwtrack-server` | the ingest endpoint |
| `fwtrack-import-influx` | import history from the older InfluxDB schema |

### Health check

```bash
fwtrack-init --project blinky
```

```
✅ database: connected
✅ schema: present
✅ data: blinky — 1516 builds (2025-08-25 .. 2026-08-24)
✅ grafana: https://fwtrack.example.com (version 13.0.1)
```

Worth running first when a dashboard is empty: it distinguishes "nothing was
recorded" from "the filters exclude everything", which look identical from the
dashboard.

### Schema changes

`deploy/schema.sql` runs once, on an empty data directory. Changing it does not
touch a database that already exists — apply an `ALTER` by hand and keep the
file in step:

```bash
docker compose exec -T postgres psql -U fwtrack -d fwtrack < migration.sql
```

### Backups

```bash
docker compose exec -T postgres pg_dump -U fwtrack fwtrack | gzip > fwtrack-$(date +%F).sql.gz
```

### Data model

```
builds          one row per build: project, time, commit, branch, version,
                origin, dirty, toolchain, and custom dimensions in a JSONB column
memory_usage    one row per region of a build: used, total
region_budgets  warning levels, per project and region
memory_points   a view joining the three, with free and percentage computed
```

Custom dimensions live in JSONB, so adding one never requires a migration.
Writes upsert on `(project, commit, built_at, tags)`, so re-running a pipeline
on the same commit refreshes the numbers instead of piling up duplicates. On a
dirty tree the build time is used instead of the commit time, so successive
local iterations stay separate.

Anything is queryable directly, and `memory_points` is the stable surface to
build custom panels on:

```sql
SELECT built_at, region, used, pcnt
FROM memory_points
WHERE project = 'blinky' AND area = 'flash'
ORDER BY built_at DESC;
```

## Requirements

Python 3.11+, a GNU toolchain that emits a linker map, Docker for the server
side. Region sizes are read from the MAP file and the layout from the ELF
program headers, so no region name is ever hardcoded: a project whose flash is
called `ROM` works like one calling it `FLASH`.

## Licence

GPL-3.0. Running it in CI imposes nothing on the firmware being measured — it is
a separate tool, not linked into anything.
