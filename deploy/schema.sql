-- Firmware memory footprint schema.
--
-- Two tables rather than one flat one: deltas and baseline comparisons are
-- computed per build, while usage is per region within a build. Anything a
-- particular project wants to slice by lives in builds.tags, so adding a
-- dimension never requires a migration.

CREATE TABLE IF NOT EXISTS builds (
    id        BIGSERIAL   PRIMARY KEY,
    project   TEXT        NOT NULL,
    built_at  TIMESTAMPTZ NOT NULL,
    commit    TEXT        NOT NULL,
    branch    TEXT        NOT NULL,
    version   TEXT,
    origin    TEXT        NOT NULL,          -- 'ci' | 'local'
    dirty     BOOLEAN     NOT NULL,
    toolchain TEXT,                          -- read from the ELF .comment section
    tags      JSONB       NOT NULL DEFAULT '{}'
);

-- Rebuilding a clean commit overwrites its point instead of piling up
-- duplicates. On a dirty tree the writer uses the build time, so successive
-- local iterations stay separate rows.
CREATE UNIQUE INDEX IF NOT EXISTS builds_identity
    ON builds (project, commit, built_at, tags);
CREATE INDEX IF NOT EXISTS builds_tags     ON builds USING GIN (tags);
CREATE INDEX IF NOT EXISTS builds_timeline ON builds (project, branch, built_at DESC);

CREATE TABLE IF NOT EXISTS memory_usage (
    build_id   BIGINT NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    region     TEXT   NOT NULL,              -- as named by the linker script
    area       TEXT   NOT NULL,              -- user-defined grouping: flash, ram, ...
    used     BIGINT NOT NULL,
    -- Nullable: history imported from a tracker that never recorded region
    -- sizes has no honest value to put here, and inventing one would make the
    -- percentages lie. The view turns a missing size into a NULL percentage.
    total    BIGINT,                         -- physical size, from the MAP file
    PRIMARY KEY (build_id, region)
);

CREATE INDEX IF NOT EXISTS memory_usage_area ON memory_usage (area);

-- Warning levels live here rather than on every row of memory_usage: they are a
-- property of a region, not of a build, and repeating the same array for every
-- build times region is a lot of duplication for a value that rarely changes.
-- What a gate needs is the current budget anyway, not the one that happened to
-- be in force six months ago.
CREATE TABLE IF NOT EXISTS region_budgets (
    project    TEXT        NOT NULL,
    region     TEXT        NOT NULL,
    thresholds SMALLINT[]  NOT NULL,          -- percentages, ascending
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project, region)
);

-- Flattens the join out of every dashboard query and keeps derived values in
-- one place. Percentages are computed on read: storing them would just be a
-- second copy of used/total that can drift.
CREATE OR REPLACE VIEW memory_points AS
SELECT b.id AS build_id,
       b.project,
       b.built_at,
       b.commit,
       b.branch,
       b.version,
       b.origin,
       b.dirty,
       b.toolchain,
       b.tags,
       m.region,
       m.area,
       m.used,
       m.total,
       COALESCE(g.thresholds, '{}') AS thresholds,
       m.total - m.used AS free,
       ROUND(100.0 * m.used / NULLIF(m.total, 0), 3) AS pcnt
FROM builds b
JOIN memory_usage m ON m.build_id = b.id
LEFT JOIN region_budgets g ON g.project = b.project AND g.region = m.region;
