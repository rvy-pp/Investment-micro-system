-- ============================================================================
-- PinPOINT IMS — core schema v1
-- ============================================================================
-- Design commitments enforced here rather than by convention, because the
-- previous system failed on exactly these points:
--
--   1. No number without a citation.  observations.quote is NOT NULL and
--      CHECKed non-empty. An extractor that cannot cite source text cannot
--      write a row.
--   2. Silence changes nothing.  There is no decay column and no scheduled
--      write path. Staleness is DERIVED (max(as_of) per entity+factor), so a
--      quiet name simply shows as stale instead of manufacturing a delta.
--   3. Scores are relative and gated.  scores carries z / rank within
--      peer_group; score_runs carries the dispersion test result, so "no
--      trade" is a first-class outcome.
--   4. Every signal has a falsifier.  signals.falsifier is NOT NULL and
--      CHECKed non-empty. This is what the review loop grades against later.
--   5. Everything is replayable.  score_runs stamps spec_version + code_sha.
--      A spec change is a re-run over history, never an in-place rewrite.
--
-- Append-only discipline: observations are never UPDATEd. A correction is a
-- new row whose supersedes_id points at the row it replaces.
--
-- STRICT tables throughout, so a text value cannot land in a numeric column.
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- L0: immutable record of every raw capture.
-- id is the sha256 of the raw bytes, so re-ingesting the same document is a
-- no-op rather than a duplicate.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,          -- sha256 of raw content
    kind          TEXT NOT NULL,             -- broker_note|broker_email|price|oi|commodity|filing|concall|ims_snapshot|manual
    origin        TEXT,                      -- broker or feed name: 'Ambit', 'NSE', 'LME'
    title         TEXT,
    source_date   TEXT NOT NULL,             -- ISO date the content is ABOUT (trade date)
    captured_at   TEXT NOT NULL,             -- ISO8601 UTC when we pulled it
    raw_path      TEXT NOT NULL,             -- data/raw/<kind>/<source_date>/<id>.<ext>
    meta          TEXT,                      -- JSON
    CHECK (source_date GLOB '____-__-__'),
    CHECK (length(trim(raw_path)) > 0)
) STRICT;

CREATE INDEX IF NOT EXISTS ix_sources_date ON sources (source_date);
CREATE INDEX IF NOT EXISTS ix_sources_kind ON sources (kind, source_date);

-- ---------------------------------------------------------------------------
-- Entities: anything an observation can be ABOUT.
--
-- Two separate ideas, deliberately not one column:
--   sector      = coverage bucket (organisational; how you divide your desk)
--   peer_group  = scoring universe (names that share a factor basis and are
--                 therefore rankable against each other)
--
-- They differ in practice. Coverage/Aluminium holds Hindustan Zinc (a zinc
-- name) and Vedanta Aluminium (an unlisted division). Cross-sectional
-- z-scoring across those is meaningless, so scoring keys on peer_group.
--
-- is_tradeable = 0 marks reporting units with no ticker (e.g. Vedanta
-- Aluminium). They may CARRY observations, but never receive a score.
-- parent_id propagates their observations to the listed parent.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entities (
    id            TEXT PRIMARY KEY,          -- stable slug: 'hindalco', 'lme_aluminium'
    kind          TEXT NOT NULL,             -- company|reporting_unit|commodity|macro|fx|index
    name          TEXT NOT NULL,
    sector        TEXT,                      -- coverage bucket; NULL for commodity/macro
    peer_group    TEXT,                      -- scoring universe; NULL = not scored
    parent_id     TEXT REFERENCES entities (id),
    is_tradeable  INTEGER NOT NULL DEFAULT 1,
    nse_symbol    TEXT,
    bloomberg     TEXT,
    isin          TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    meta          TEXT,                      -- JSON
    CHECK (kind IN ('company','reporting_unit','commodity','macro','fx','index')),
    CHECK (is_tradeable IN (0,1)),
    CHECK (active IN (0,1)),
    -- a scoreable name must be tradeable; an untradeable unit must have a parent
    CHECK (peer_group IS NULL OR is_tradeable = 1),
    CHECK (is_tradeable = 1 OR parent_id IS NOT NULL)
) STRICT;

CREATE INDEX IF NOT EXISTS ix_entities_peer ON entities (peer_group, active);
CREATE INDEX IF NOT EXISTS ix_entities_sector ON entities (sector, active);

-- ---------------------------------------------------------------------------
-- L1: THE fact table. Append-only.
--
-- factor  = which factor in the peer_group spec this feeds ('input_cost')
-- metric  = the specific thing measured ('alumina_spot')
-- One factor has many metrics; the spec maps metrics -> factor contribution.
--
-- direction is the extractor's read of sign-of-impact ON THIS ENTITY, which
-- is NOT a property of the metric alone: a rising alumina price is negative
-- for Hindalco and positive for NALCO. Signed exposure lives in the entity
-- spec; this column records what the source text actually asserted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS observations (
    id                INTEGER PRIMARY KEY,
    source_id         TEXT NOT NULL REFERENCES sources (id),
    entity_id         TEXT NOT NULL REFERENCES entities (id),
    as_of             TEXT NOT NULL,         -- ISO date the fact is ABOUT
    factor            TEXT NOT NULL,
    metric            TEXT NOT NULL,
    value_num         REAL,
    value_text        TEXT,                  -- categorical: 'BUY', 'upgrade'
    unit              TEXT,                  -- 'USD/t', '%', 'x', 'INR/sh'
    period            TEXT,                  -- 'spot', 'Q1FY27', 'FY27', '2026-07'
    direction         INTEGER,               -- -1 | 0 | +1 as asserted by the source
    confidence        REAL NOT NULL,
    quote             TEXT NOT NULL,         -- VERBATIM source text. commitment #1
    extractor_version TEXT NOT NULL,
    supersedes_id     INTEGER REFERENCES observations (id),
    created_at        TEXT NOT NULL,
    CHECK (length(trim(quote)) > 0),                      -- no number without a citation
    CHECK (value_num IS NOT NULL OR value_text IS NOT NULL),
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CHECK (direction IS NULL OR direction IN (-1, 0, 1)),
    CHECK (as_of GLOB '____-__-__')
) STRICT;

CREATE INDEX IF NOT EXISTS ix_obs_entity_asof ON observations (entity_id, as_of);
CREATE INDEX IF NOT EXISTS ix_obs_factor      ON observations (factor, as_of);
CREATE INDEX IF NOT EXISTS ix_obs_source      ON observations (source_id);
-- staleness is derived from this index, never stored
CREATE INDEX IF NOT EXISTS ix_obs_stale       ON observations (entity_id, factor, as_of DESC);

-- ---------------------------------------------------------------------------
-- Market data. Kept out of observations because it is not model-extracted:
-- it needs no citation and arrives as complete series.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prices (
    entity_id  TEXT NOT NULL REFERENCES entities (id),
    date       TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL NOT NULL,
    volume     REAL,
    currency   TEXT,
    PRIMARY KEY (entity_id, date),
    CHECK (date GLOB '____-__-__')
) STRICT;

CREATE TABLE IF NOT EXISTS oi (
    entity_id      TEXT NOT NULL REFERENCES entities (id),
    date           TEXT NOT NULL,
    expiry         TEXT,
    oi             REAL,
    oi_chg_pct     REAL,
    price_chg_pct  REAL,
    buildup        TEXT,                     -- long_buildup|short_buildup|short_covering|long_unwinding
    oi_percentile  REAL,                     -- position of today's OI in its own history
    lookback_days  INTEGER,                  -- window the percentile was computed over
    PRIMARY KEY (entity_id, date),
    CHECK (buildup IS NULL OR buildup IN
        ('long_buildup','short_buildup','short_covering','long_unwinding','neutral')),
    CHECK (oi_percentile IS NULL OR (oi_percentile >= 0.0 AND oi_percentile <= 100.0))
) STRICT;

-- ---------------------------------------------------------------------------
-- Broker behaviour — for the consensus / crowding read.
-- Stored structurally rather than as prose so "who is offside" is a query.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS broker_actions (
    id           INTEGER PRIMARY KEY,
    source_id    TEXT NOT NULL REFERENCES sources (id),
    entity_id    TEXT NOT NULL REFERENCES entities (id),
    broker       TEXT NOT NULL,
    action_date  TEXT NOT NULL,
    action       TEXT NOT NULL,              -- initiate|upgrade|downgrade|reiterate|tp_change|drop
    rating_from  TEXT,
    rating_to    TEXT,
    tp_from      REAL,
    tp_to        REAL,
    currency     TEXT,
    quote        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    CHECK (length(trim(quote)) > 0),
    CHECK (action IN ('initiate','upgrade','downgrade','reiterate','tp_change','drop')),
    CHECK (action_date GLOB '____-__-__')
) STRICT;

CREATE INDEX IF NOT EXISTS ix_broker_entity ON broker_actions (entity_id, action_date);
CREATE INDEX IF NOT EXISTS ix_broker_broker ON broker_actions (broker, action_date);

-- ---------------------------------------------------------------------------
-- Estimates — feeds the Projections tab. One row per broker/period/metric,
-- so revision breadth and dispersion are both computable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS estimates (
    id          INTEGER PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES sources (id),
    entity_id   TEXT NOT NULL REFERENCES entities (id),
    broker      TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    period      TEXT NOT NULL,               -- 'Q1FY27', 'FY27'
    metric      TEXT NOT NULL,              -- revenue|ebitda|ebitda_per_t|pat|eps|volume|realisation
    value_num   REAL NOT NULL,
    unit        TEXT,
    quote       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    CHECK (length(trim(quote)) > 0),
    CHECK (as_of GLOB '____-__-__')
) STRICT;

CREATE INDEX IF NOT EXISTS ix_est_entity ON estimates (entity_id, period, metric, as_of);

-- ---------------------------------------------------------------------------
-- The book. pair_id is load-bearing: a long/short desk reconciles at pair
-- level, so the review layer joins on it, not only on single names.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    id             INTEGER PRIMARY KEY,
    snapshot_date  TEXT NOT NULL,
    entity_id      TEXT REFERENCES entities (id),   -- NULL until ticker is mapped
    raw_ticker     TEXT NOT NULL,
    instrument     TEXT NOT NULL,            -- future|cash|option
    pair_id        TEXT,
    side           TEXT,                     -- LONG|SHORT
    qty            REAL,
    cost           REAL,
    mv_pct         REAL,                     -- fraction of NAV
    beta_mv_pct    REAL,                     -- true directional exposure incl. option delta
    gmv_pct        REAL,
    dtd_pnl        REAL,
    mtd_pnl        REAL,
    ytd_pnl        REAL,
    nav            REAL,
    currency       TEXT,
    created_at     TEXT NOT NULL,
    CHECK (instrument IN ('future','cash','option')),
    CHECK (side IS NULL OR side IN ('LONG','SHORT')),
    CHECK (snapshot_date GLOB '____-__-__')
) STRICT;

CREATE INDEX IF NOT EXISTS ix_pos_date ON positions (snapshot_date);
CREATE INDEX IF NOT EXISTS ix_pos_pair ON positions (pair_id, snapshot_date);

-- ---------------------------------------------------------------------------
-- L3: scoring. One score_run per (as_of, peer_group, spec_version).
--
-- dispersion_ok is the gate: when the cross-sectional spread is inside the
-- noise band the run is still recorded, but the correct read is "no trade"
-- rather than a ranking of near-identical numbers.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS score_runs (
    id             INTEGER PRIMARY KEY,
    as_of          TEXT NOT NULL,
    peer_group     TEXT NOT NULL,
    spec_version   TEXT NOT NULL,
    code_sha       TEXT NOT NULL,            -- git sha of the scoring engine
    dispersion     REAL,                     -- realised cross-sectional spread
    dispersion_min REAL,                     -- threshold from the spec
    dispersion_ok  INTEGER NOT NULL,
    n_scored       INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (as_of, peer_group, spec_version),
    CHECK (dispersion_ok IN (0,1)),
    CHECK (as_of GLOB '____-__-__')
) STRICT;

-- Per-entity, per-factor decomposition. Storing the factor level (not just
-- the composite) is what makes a score explainable instead of a verdict.
CREATE TABLE IF NOT EXISTS scores (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES score_runs (id) ON DELETE CASCADE,
    entity_id     TEXT NOT NULL REFERENCES entities (id),
    factor        TEXT NOT NULL,             -- '_composite' for the roll-up
    raw           REAL,                      -- factor level in native terms
    z             REAL,                      -- z within peer_group at as_of
    rank          INTEGER,                   -- 1 = best in peer_group
    n_obs         INTEGER NOT NULL,          -- observations behind this number
    stale_days    INTEGER,                   -- age of the newest supporting obs
    weight        REAL,                      -- signed exposure from entity spec
    UNIQUE (run_id, entity_id, factor)
) STRICT;

CREATE INDEX IF NOT EXISTS ix_scores_entity ON scores (entity_id, factor);

-- ---------------------------------------------------------------------------
-- Signals: the directional output. This is the layer the old system lacked —
-- it stopped at a score and never resolved to a position-relevant statement.
--
-- falsifier is NOT NULL by design (commitment #4). A signal you cannot
-- disprove cannot be graded, and grading is what the review loop runs on.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES score_runs (id) ON DELETE CASCADE,
    as_of         TEXT NOT NULL,
    kind          TEXT NOT NULL,             -- single|pair
    long_entity   TEXT REFERENCES entities (id),
    short_entity  TEXT REFERENCES entities (id),
    direction     TEXT NOT NULL,             -- long|short|spread|flat
    conviction    TEXT NOT NULL,             -- low|medium|high
    thesis        TEXT NOT NULL,             -- one sentence, cites the driving factor
    falsifier     TEXT NOT NULL,             -- 'wrong if alumina holds above 340'
    driving_factor TEXT,                     -- which factor moved it
    created_at    TEXT NOT NULL,
    CHECK (length(trim(falsifier)) > 0),
    CHECK (length(trim(thesis)) > 0),
    CHECK (kind IN ('single','pair')),
    CHECK (direction IN ('long','short','spread','flat')),
    CHECK (conviction IN ('low','medium','high')),
    CHECK (kind = 'single' OR (long_entity IS NOT NULL AND short_entity IS NOT NULL))
) STRICT;

CREATE INDEX IF NOT EXISTS ix_signals_asof ON signals (as_of);

-- ---------------------------------------------------------------------------
-- L5: outcome grading. Written only once forward return is knowable, which
-- is why the previous Priors file never accumulated a single promoted rule —
-- nothing ever joined a signal to what happened next.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcomes (
    id           INTEGER PRIMARY KEY,
    signal_id    INTEGER NOT NULL REFERENCES signals (id) ON DELETE CASCADE,
    horizon_days INTEGER NOT NULL,
    graded_at    TEXT NOT NULL,
    ret_long     REAL,
    ret_short    REAL,
    ret_spread   REAL,                       -- the number that matters for a pair
    verdict      TEXT NOT NULL,              -- right|wrong|inconclusive
    falsifier_hit INTEGER,                   -- did the stated falsifier trigger?
    note         TEXT,
    UNIQUE (signal_id, horizon_days),
    CHECK (verdict IN ('right','wrong','inconclusive')),
    CHECK (falsifier_hit IS NULL OR falsifier_hit IN (0,1))
) STRICT;

-- ---------------------------------------------------------------------------
-- Derived views
-- ---------------------------------------------------------------------------

-- Staleness, computed rather than stored. Replaces decay entirely: a quiet
-- name shows an increasing stale_days and an unchanged score.
CREATE VIEW IF NOT EXISTS v_factor_freshness AS
SELECT entity_id,
       factor,
       MAX(as_of)      AS last_confirmed,
       COUNT(*)        AS n_obs,
       julianday('now') - julianday(MAX(as_of)) AS stale_days
FROM observations
WHERE supersedes_id IS NULL
GROUP BY entity_id, factor;

-- Current live observations only (corrections applied).
CREATE VIEW IF NOT EXISTS v_observations_live AS
SELECT o.*
FROM observations o
WHERE NOT EXISTS (
    SELECT 1 FROM observations c WHERE c.supersedes_id = o.id
);
