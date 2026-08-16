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
--      in-flavour/out-of-flavour permission layer and coverage_ok flags a
--      bridge with too many unpriced lines, so "no trade" and "cannot tell"
--      are both first-class outcomes rather than a weak lean.
--   4. Every signal has a falsifier.  signals.falsifier is NOT NULL and
--      CHECKed non-empty. This is what the review loop grades against later.
--   5. Everything is replayable.  bridge_runs and signals stamp spec_version
--      + code_sha. A spec change is a re-run over history, never a rewrite.
--
-- FOUR PILLARS, deliberately not more. A ten-factor model overfits and goes
-- rigid; these four are what actually move a stock:
--
--   P1  ASP        what they realise: product mix x volume x price vs benchmark
--   P2  COSTS      what they consume: dominant input lines x intensity x sourcing
--   P3  VALUATION  where it trades vs its own history (a holdco discount is
--                  just one more metric here, not its own model)
--   P4  GUIDANCE   will management hit next quarter? own track record as the
--                  prior, daily events as evidence
--
-- P1 and P2 together are the margin bridge and give DIRECTION and SIZE.
-- P3 gives CONVICTION. P4 gives the FORWARD view. Sector regime (in flavour /
-- out of flavour) gates whether any of it can express.
--
-- Keep only the input lines that move the needle. For a smelter that is
-- alumina and power; adding six more reagents adds parameters, not accuracy.
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
    CHECK (source_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
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
    CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
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
    CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
) STRICT;

CREATE TABLE IF NOT EXISTS oi (
    entity_id      TEXT NOT NULL REFERENCES entities (id),
    date           TEXT NOT NULL,
    expiry         TEXT,
    oi             REAL,                     -- futures OI in LOTS
    oi_chg_lots    REAL,                     -- as published; pct is derived, not assumed
    oi_chg_pct     REAL,
    price          REAL,
    price_chg_pct  REAL,
    lot_size       INTEGER,
    -- Two horizons, because they routinely disagree and the disagreement is the
    -- signal: a name can be short-covering over 15d inside a 3m short build.
    buildup        TEXT,                     -- 3-month read
    buildup_15d    TEXT,
    oi_percentile  REAL,                     -- 3-month percentile
    oi_percentile_15d REAL,
    z_score_3m     REAL,
    pct_vs_median_3m REAL,
    lookback_days  INTEGER,
    source         TEXT,                     -- provenance: 'vault_oi_history'
    PRIMARY KEY (entity_id, date),
    CHECK (buildup_15d IS NULL OR buildup_15d IN
        ('long_buildup','short_buildup','short_covering','long_unwinding','neutral')),
    CHECK (buildup IS NULL OR buildup IN
        ('long_buildup','short_buildup','short_covering','long_unwinding','neutral')),
    CHECK (oi_percentile IS NULL OR (oi_percentile >= 0.0 AND oi_percentile <= 100.0))
) STRICT;

-- ---------------------------------------------------------------------------
-- PM overrides — the front-end's write path.
--
-- The YAML specs are the checked-in BASELINE and stay in git. Desk corrections
-- land here instead of rewriting YAML from a web form, for three reasons:
-- a form-driven file rewrite can corrupt a spec, the DB keeps who/when/why,
-- and the baseline stays diffable so it is always clear what was changed from
-- the analyst's original and by whom.
--
-- The bridge reads YAML, then applies any override on top. Deleting a row
-- reverts cleanly to the spec.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS overrides (
    id          INTEGER PRIMARY KEY,
    entity_id   TEXT NOT NULL,
    scope       TEXT NOT NULL,        -- 'output' | 'input' | 'financial'
    item        TEXT,                 -- line item; NULL for entity-level financials
    field       TEXT NOT NULL,        -- 'volume','market_pct','intensity','base_ebitda',...
    value_num   REAL NOT NULL,
    prev_value  REAL,                 -- what the spec said, so the delta is visible
    note        TEXT,                 -- why. optional but strongly encouraged
    author      TEXT NOT NULL DEFAULT 'pm',
    created_at  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    CHECK (scope IN ('output','input','financial')),
    CHECK (active IN (0,1))
) STRICT;

CREATE INDEX IF NOT EXISTS ix_ovr ON overrides (entity_id, scope, item, field, active);

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
    CHECK (action_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
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
    CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
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
    CHECK (snapshot_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
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
    CHECK (effective_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
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
    CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
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
-- PILLAR 3 — VALUATION (where it trades)
-- ===========================================================================
-- Valuation, mood, consensus, flows. Never sets direction; it decides whether
-- an ASP/cost move is already in the price, and therefore size and conviction.
--
-- A holdco discount is just another valuation metric here. It does not get its
-- own model — an earlier draft built a whole sum-of-parts NAV layer for it,
-- which was complexity the desk did not ask for.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_layer (
    entity_id           TEXT NOT NULL REFERENCES entities (id),
    as_of               TEXT NOT NULL,
    ev_ebitda           REAL,
    pe                  REAL,
    pb                  REAL,
    ev_ebitda_pctile    REAL,                -- vs its OWN history, not vs peers
    holdco_discount_pct REAL,                -- only meaningful for holdcos; just a metric
    holdco_discount_pctile REAL,
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
    CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (ev_ebitda_pctile IS NULL OR (ev_ebitda_pctile >= 0.0 AND ev_ebitda_pctile <= 100.0))
) STRICT;

-- ===========================================================================
-- PILLAR 4 — GUIDANCE CONFIDENCE (what next quarter looks like)
-- ===========================================================================
-- "Is management going to hit what they said?" — answered from two things:
--   a PRIOR  : this company's own history of meeting or missing its guidance
--   EVIDENCE : daily events that support or undermine the current commitment
--
-- This is what makes the system forward-looking rather than a description of
-- what already happened. It is also the only pillar where being early is the
-- point: by the time a miss is reported it is in the price.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS guidance (
    id            INTEGER PRIMARY KEY,
    entity_id     TEXT NOT NULL REFERENCES entities (id),
    source_id     TEXT NOT NULL REFERENCES sources (id),
    issued_date   TEXT NOT NULL,             -- when management said it
    period        TEXT NOT NULL,             -- 'Q2FY27', 'FY27'
    metric        TEXT NOT NULL,             -- volume|ebitda_per_t|cost_per_t|capex|margin
    target_type   TEXT NOT NULL,             -- point|range|direction
    target_value  REAL,                      -- for point
    target_low    REAL,                      -- for range
    target_high   REAL,
    target_dir    TEXT,                      -- for direction: up|down|flat
    unit          TEXT,
    quote         TEXT NOT NULL,             -- verbatim management statement
    status        TEXT NOT NULL DEFAULT 'open',   -- open|met|missed|withdrawn
    resolved_date TEXT,
    actual_value  REAL,
    created_at    TEXT NOT NULL,
    CHECK (length(trim(quote)) > 0),
    CHECK (target_type IN ('point','range','direction')),
    CHECK (status IN ('open','met','missed','withdrawn')),
    CHECK (target_dir IS NULL OR target_dir IN ('up','down','flat')),
    -- a target must actually state something
    CHECK ((target_type = 'point'     AND target_value IS NOT NULL)
        OR (target_type = 'range'     AND target_low IS NOT NULL AND target_high IS NOT NULL)
        OR (target_type = 'direction' AND target_dir IS NOT NULL)),
    CHECK (issued_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
) STRICT;

CREATE INDEX IF NOT EXISTS ix_guid_entity ON guidance (entity_id, period, status);

-- Daily events that move confidence in an open commitment. Cited, like
-- everything else the model extracts.
CREATE TABLE IF NOT EXISTS guidance_evidence (
    id          INTEGER PRIMARY KEY,
    guidance_id INTEGER NOT NULL REFERENCES guidance (id) ON DELETE CASCADE,
    source_id   TEXT NOT NULL REFERENCES sources (id),
    as_of       TEXT NOT NULL,
    direction   INTEGER NOT NULL,            -- +1 supports, -1 undermines
    weight      REAL NOT NULL,               -- 0..1, how much it should move confidence
    quote       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    CHECK (length(trim(quote)) > 0),
    CHECK (direction IN (-1, 1)),            -- neutral evidence is not evidence
    CHECK (weight > 0.0 AND weight <= 1.0),
    CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
) STRICT;

CREATE INDEX IF NOT EXISTS ix_gev_guidance ON guidance_evidence (guidance_id, as_of);

-- Computed: probability this commitment is met, and the track record behind it.
CREATE TABLE IF NOT EXISTS guidance_confidence (
    entity_id         TEXT NOT NULL REFERENCES entities (id),
    period            TEXT NOT NULL,
    as_of             TEXT NOT NULL,
    confidence        REAL NOT NULL,         -- 0..1
    track_record_rate REAL,                  -- own historical hit rate = the prior
    track_record_n    INTEGER,               -- how many resolved commitments back it
    n_evidence_for    INTEGER NOT NULL,
    n_evidence_against INTEGER NOT NULL,
    spec_version      TEXT NOT NULL,
    code_sha          TEXT NOT NULL,
    PRIMARY KEY (entity_id, period, as_of),
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
) STRICT;

-- Track record, derived rather than stored: a company that habitually misses
-- should not get the benefit of the doubt on its next commitment.
CREATE VIEW IF NOT EXISTS v_guidance_track_record AS
SELECT entity_id,
       COUNT(*)                                                   AS n_resolved,
       SUM(CASE WHEN status = 'met' THEN 1 ELSE 0 END)            AS n_met,
       CAST(SUM(CASE WHEN status = 'met' THEN 1 ELSE 0 END) AS REAL)
           / NULLIF(COUNT(*), 0)                                  AS hit_rate
FROM guidance
WHERE status IN ('met', 'missed')
GROUP BY entity_id;

-- ===========================================================================
-- GATE — SECTOR REGIME (in flavour / out of flavour)
-- ===========================================================================
-- A permission layer, not another additive term. Positive news into a sector
-- with no investor interest does not move stocks, so `can_express` gates
-- whether a Layer 1+2 conclusion is allowed to become a position.
--
-- FOUR STATES, and `out_of_flavour` vs `ignored` is a real distinction rather
-- than a gradation:
--   in_flavour      bid. good news is rewarded, longs work
--   out_of_flavour  actively sold. shorts work — there IS interest, it is
--                   just negative
--   neutral         no strong pull either way
--   ignored         NO interest at all. Nothing expresses, in either
--                   direction, however good the economics
-- A sector being sold and a sector being ignored need opposite handling, so
-- they must not collapse into one "bad" state.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sector_regime (
    sector           TEXT NOT NULL,
    as_of            TEXT NOT NULL,
    state            TEXT NOT NULL,          -- in_flavour|out_of_flavour|neutral|ignored
    breadth_pct      REAL,                   -- % of sector names above their 50dma
    rel_strength     REAL,                   -- sector vs index, normalised
    turnover_pctile  REAL,                   -- investor-interest proxy vs own history
    flow_fii         REAL,
    dispersion       REAL,                   -- intra-sector return dispersion
    can_express      INTEGER NOT NULL,       -- the gate
    note             TEXT,
    PRIMARY KEY (sector, as_of),
    CHECK (state IN ('in_flavour','out_of_flavour','neutral','ignored')),
    CHECK (can_express IN (0,1)),
    CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
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
    l2_priced_in    TEXT,                    -- P3 valuation: not_priced|partly|priced
    p4_guidance_conf REAL,                   -- P4: confidence management hits the quarter
    l3_regime       TEXT,                    -- regime state at emission
    l3_gated        INTEGER NOT NULL DEFAULT 0,  -- 1 = held back by the flavour gate
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
    CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
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
