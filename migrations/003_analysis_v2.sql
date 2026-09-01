-- Analysis v2, phase 1: the data foundation.
--
-- creators: one row per Instagram account we have seen a reel from, with the
--   follower count that turns raw views into a size-normalised "reach multiple"
--   (views / followers). A reel that did 3x its creator's follower count travelled
--   to non-followers - that is true at any account size, which is what lets a
--   formula drawn from a 5M-follower account say anything about a 50k one.
--   Refreshed when older than CREATOR_REFRESH_DAYS (app.py); the follower count
--   in force when a reel's stats were fetched is also snapshotted into reels.meta
--   so compute_metrics() stays a pure function over the job.
--
-- reels.source: 'own' when the reel is from the account this project is about
--   (projects.own_username), 'reference' for everything saved from elsewhere. The
--   agent's "why didn't MY reel work" needs this split; set automatically from
--   meta.username on every stats fetch and re-applied when own_username changes.
-- reels.caption / hashtags: the caption is the text hook and the topic signal.
--   Parsed from the media response we already fetch for counts.
-- reels.tags / diagnosis: written by the phase-2 Reel Card pass (structured
--   taxonomy tags + a per-reel diagnosis). Null until then.
-- reels.insights: hand-entered IG Insights for own reels (avg watch time,
--   retention, non-follower %, saves ...) - not readable from any public endpoint.
--
-- projects.own_username: which account is "me" in this project, if any.
-- projects.outcome_mode: how the outcome band is decided for the analysis prompt -
--   'views' = absolute view thresholds (GOOD/AMAZING_VIEW_THRESHOLD), 'reach' =
--   reach-multiple bands. 'views' stays the default.
--
-- formulas.levers / effect / lift / taxonomy_v: phase-3 columns, added now so the
--   schema is settled; null until the formula run writes them.

BEGIN;

CREATE TABLE IF NOT EXISTS creators (
  username text PRIMARY KEY,
  ig_user_id text,
  full_name text,
  follower_count int,
  following_count int,
  media_count int,
  is_verified boolean,
  fetched_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE reels ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'reference';
ALTER TABLE reels DROP CONSTRAINT IF EXISTS reels_source_check;
ALTER TABLE reels ADD CONSTRAINT reels_source_check CHECK (source IN ('own', 'reference'));
ALTER TABLE reels ADD COLUMN IF NOT EXISTS caption text;
ALTER TABLE reels ADD COLUMN IF NOT EXISTS hashtags text[];
ALTER TABLE reels ADD COLUMN IF NOT EXISTS tags jsonb;
ALTER TABLE reels ADD COLUMN IF NOT EXISTS diagnosis jsonb;
ALTER TABLE reels ADD COLUMN IF NOT EXISTS insights jsonb;
CREATE INDEX IF NOT EXISTS reels_tags_gin ON reels USING gin (tags);

ALTER TABLE projects ADD COLUMN IF NOT EXISTS own_username text;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS outcome_mode text NOT NULL DEFAULT 'views';
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_outcome_mode_check;
ALTER TABLE projects ADD CONSTRAINT projects_outcome_mode_check CHECK (outcome_mode IN ('views', 'reach'));

ALTER TABLE formulas ADD COLUMN IF NOT EXISTS levers jsonb;
ALTER TABLE formulas ADD COLUMN IF NOT EXISTS effect text;
ALTER TABLE formulas DROP CONSTRAINT IF EXISTS formulas_effect_check;
ALTER TABLE formulas ADD CONSTRAINT formulas_effect_check CHECK (effect IN ('positive', 'neutral', 'negative'));
ALTER TABLE formulas ADD COLUMN IF NOT EXISTS lift numeric;
ALTER TABLE formulas ADD COLUMN IF NOT EXISTS taxonomy_v int;

COMMIT;
