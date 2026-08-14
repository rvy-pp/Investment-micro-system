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
--   3. Output is gated, not always-on.  sector_regime.can_express is the
--      tezi/mandi permission layer and bridge_results.coverage_ok flags a
--      bridge with too many unpriced lines, so "no trade" and "cannot tell"
--      are both first-class outcomes rather than a weak lean.
--   4. Every signal has a falsifier.  signals.falsifier is NOT NULL and
--      CHECKed non-empty. This is what the review loop grades against later.
--   5. Everything is replayable.  bridge_runs and signals stamp spec_version
--      + code_sha. A spec change is a re-run over history, never a rewrite.
--
-- There is NO generic factor-weight model. Companies do not share a factor
-- structure, they share an arithmetic: cost stack x intensity x sourcing,
-- against product mix x volume x ASP. See the LAYER 1 section.
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

-- ===========================================================================
-- LAYER 1 — COMPANY ECONOMICS
-- ===========================================================================
-- There is deliberately no generic factor-weight model here. Companies do not
-- share a factor structure; they share an ARITHMETIC. What differs is each
-- company's cost stack, product mix, and how much of each input it actually
-- buys at market.
--
-- `market_pct` is the load-bearing field. It is why one alumina print moves
-- three aluminium names in three different directions:
--
--   NALCO  smelter alumina market_pct ~0.0  (own refinery, internal transfer)
--          PLUS an alumina_surplus OUTPUT line marked to market
--          => alumina up is REVENUE
--   VAML   smelter alumina market_pct ~0.5  (bauxite-short, buys third-party)
--          no surplus output line
--          => alumina up is COST
--   HNDL   broadly self-sufficient, small surplus line
--          => alumina up is roughly NEUTRAL, slightly positive
--
-- None of that is asserted as a coefficient. It falls out of the structure.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS economics (
    id             INTEGER PRIMARY KEY,
    entity_id      TEXT NOT NULL REFERENCES entities (id),
    effective_from TEXT NOT NULL,            -- structure changes with capex/mix
    effective_to   TEXT,                     -- NULL = current
    line_kind      TEXT NOT NULL,            -- output|input
    item           TEXT NOT NULL,            -- 'aluminium_ingot','alumina_surplus','power'
    price_link     TEXT REFERENCES entities (id),   -- commodity entity driving this line

    -- OUTPUT lines: what they sell
    volume         REAL,                     -- annual volume in volume_unit
    volume_unit    TEXT,                     -- 't', 'kt', 'kWh'
    mix_pct        REAL,                     -- share of total output volume
    asp_premium    REAL,                     -- premium/discount vs the linked benchmark
    asp_premium_unit TEXT,                   -- 'USD/t', 'INR/t'

    -- INPUT lines: what they consume
    intensity      REAL,                     -- units of item per unit of basis_item
    intensity_unit TEXT,                     -- 't/t', 'kWh/t', 'kg/t'
    basis_item     TEXT,                     -- WHICH output the intensity is per unit of.
                                             -- Required for multi-product companies: NALCO's
                                             -- caustic soda scales with alumina tonnes while
                                             -- its carbon anode scales with metal tonnes.
                                             -- Getting this wrong misprices the whole line.
    market_pct     REAL,                     -- 0 = fully captive, 1 = fully bought at market

    currency       TEXT,
    source_note    TEXT NOT NULL,            -- provenance of the number, incl. verification state
    spec_version   TEXT NOT NULL,
    CHECK (line_kind IN ('output','input')),
    CHECK (market_pct IS NULL OR (market_pct >= 0.0 AND market_pct <= 1.0)),
    CHECK (mix_pct    IS NULL OR (mix_pct    >= 0.0 AND mix_pct    <= 1.0)),
    CHECK (length(trim(source_note)) > 0),   -- an intensity with no provenance is a guess
    CHECK (effective_from GLOB '____-__-__'),
    -- an output line needs a volume or a mix; an input line needs an intensity
    -- AND the basis that intensity is measured against
    CHECK ((line_kind = 'output' AND (volume IS NOT NULL OR mix_pct IS NOT NULL))
        OR (line_kind = 'input'  AND intensity IS NOT NULL AND basis_item IS NOT NULL))
) STRICT;

CREATE INDEX IF NOT EXISTS ix_econ_entity ON economics (entity_id, line_kind, effective_from);

-- L1 output: the margin bridge. One run per (entity, as_of, window).
CREATE TABLE IF NOT EXISTS bridge_runs (
    id           INTEGER PRIMARY KEY,
    entity_id    TEXT NOT NULL REFERENCES entities (id),
    as_of        TEXT NOT NULL,
    window_days  INTEGER NOT NULL,           -- price-change window the bridge is run over
    spec_version TEXT NOT NULL,
    code_sha     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (entity_id, as_of, window_days, spec_version),
    CHECK (as_of GLOB '____-__-__')
) STRICT;

-- Per-line decomposition — so "why did margin move" is answerable by line item,
-- not by a factor label. This is what makes the output directional.
CREATE TABLE IF NOT EXISTS bridge_lines (
    id                INTEGER PRIMARY KEY,
    run_id            INTEGER NOT NULL REFERENCES bridge_runs (id) ON DELETE CASCADE,
    line_kind         TEXT NOT NULL,
    item              TEXT NOT NULL,
    price_from        REAL,
    price_to          REAL,
    price_unit        TEXT,
    delta_ebitda      REAL,                  -- absolute, reporting currency
    delta_ebitda_per_t REAL,
    priced            INTEGER NOT NULL,      -- 0 = no price series, line skipped
    UNIQUE (run_id, line_kind, item),
    CHECK (priced IN (0,1)),
    CHECK (line_kind IN ('output','input'))
) STRICT;

CREATE TABLE IF NOT EXISTS bridge_results (
    run_id             INTEGER PRIMARY KEY REFERENCES bridge_runs (id) ON DELETE CASCADE,
    delta_revenue      REAL,
    delta_cost         REAL,
    delta_ebitda       REAL,
    delta_ebitda_per_t REAL,
    delta_margin_bps   REAL,
    base_ebitda        REAL,
    pct_of_ebitda      REAL,                 -- materiality; decides whether this matters at all
    n_lines_priced     INTEGER NOT NULL,
    n_lines_total      INTEGER NOT NULL,
    coverage_ok        INTEGER NOT NULL,     -- 0 = too many unpriced lines to trust the bridge
    CHECK (coverage_ok IN (0,1))
) STRICT;

-- ===========================================================================
-- LAYER 2 — IS IT PRICED IN
-- ===========================================================================
-- Valuation, mood, consensus, flows. Never sets direction; it decides whether
-- a Layer 1 move is already in the price, and therefore size and conviction.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_layer (
    entity_id           TEXT NOT NULL REFERENCES entities (id),
    as_of               TEXT NOT NULL,
    ev_ebitda           REAL,
    pe                  REAL,
    pb                  REAL,
    ev_ebitda_pctile    REAL,                -- vs its OWN history, not vs peers
    valuation_lookback_days INTEGER,
    consensus_net_rating REAL,               -- +1 all buy .. -1 all sell
    consensus_tp_gap_pct REAL,               -- upside to consensus TP
    coverage_count      INTEGER,
    oi_percentile       REAL,
    oi_buildup          TEXT,
    flow_fii            REAL,
    flow_dii            REAL,
    mood                TEXT,                -- derived label, never additive
    PRIMARY KEY (entity_id, as_of),
    CHECK (as_of GLOB '____-__-__'),
    CHECK (ev_ebitda_pctile IS NULL OR (ev_ebitda_pctile >= 0.0 AND ev_ebitda_pctile <= 100.0))
) STRICT;

-- ===========================================================================
-- LAYER 3 — SECTOR REGIME (tezi / mandi)
-- ===========================================================================
-- A permission layer, not another additive term. Positive news into a sector
-- with no investor interest does not move stocks, so `can_express` gates
-- whether a Layer 1+2 conclusion is allowed to become a position.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sector_regime (
    sector           TEXT NOT NULL,
    as_of            TEXT NOT NULL,
    state            TEXT NOT NULL,          -- tezi|mandi|neutral|dead
    breadth_pct      REAL,                   -- % of sector names above their 50dma
    rel_strength     REAL,                   -- sector vs index, normalised
    turnover_pctile  REAL,                   -- investor-interest proxy vs own history
    flow_fii         REAL,
    dispersion       REAL,                   -- intra-sector return dispersion
    can_express      INTEGER NOT NULL,       -- the gate
    note             TEXT,
    PRIMARY KEY (sector, as_of),
    CHECK (state IN ('tezi','mandi','neutral','dead')),
    CHECK (can_express IN (0,1)),
    CHECK (as_of GLOB '____-__-__')
) STRICT;

-- ===========================================================================
-- OUTPUT — signals
-- ===========================================================================
-- The layer the old system lacked: it stopped at a score and never resolved
-- to a position-relevant statement.
--
-- Every signal records which bridge produced it, whether Layer 2 said it was
-- already priced, and what regime Layer 3 was in. falsifier is NOT NULL by
-- design — a signal you cannot disprove cannot be graded, and grading is what
-- the review loop runs on.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY,
    as_of           TEXT NOT NULL,
    kind            TEXT NOT NULL,           -- single|pair
    long_entity     TEXT REFERENCES entities (id),
    short_entity    TEXT REFERENCES entities (id),
    direction       TEXT NOT NULL,           -- long|short|spread|flat
    conviction      TEXT NOT NULL,           -- low|medium|high
    thesis          TEXT NOT NULL,           -- one sentence, cites the driving LINE ITEM
    falsifier       TEXT NOT NULL,           -- 'wrong if alumina holds above 340'
    driving_item    TEXT,                    -- the economics line that moved it
    l1_bridge_run   INTEGER REFERENCES bridge_runs (id),
    l1_pct_of_ebitda REAL,                   -- materiality from Layer 1
    l2_priced_in    TEXT,                    -- not_priced|partly|priced
    l3_regime       TEXT,                    -- regime state at emission
    l3_gated        INTEGER NOT NULL DEFAULT 0,  -- 1 = held back by the tezi/mandi gate
    spec_version    TEXT NOT NULL,
    code_sha        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    CHECK (length(trim(falsifier)) > 0),
    CHECK (length(trim(thesis)) > 0),
    CHECK (kind IN ('single','pair')),
    CHECK (direction IN ('long','short','spread','flat')),
    CHECK (conviction IN ('low','medium','high')),
    CHECK (l2_priced_in IS NULL OR l2_priced_in IN ('not_priced','partly','priced')),
    CHECK (l3_gated IN (0,1)),
    CHECK (kind = 'single' OR (long_entity IS NOT NULL AND short_entity IS NOT NULL)),
    CHECK (as_of GLOB '____-__-__')
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
