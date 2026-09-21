-- My Instagram + Jev.
--
-- settings.jev_api_key: the account's own TypeSafe (Jev) key. Jev is the
--   judgement layer - closed-vocabulary tags on reel cards, style-match scoring
--   of rewrites. Blank falls back to the platform TYPESAFE_API_KEY, and with
--   neither set every Jev step is skipped and the LLM-only path runs as before.
--
-- creator_profiles: one row per app user - the Instagram account they connected
--   as "me". It is user-level, not project-level, because the style it holds is
--   used from every project ("rewrite this reference reel in my style").
--     project_id  the project the account's reels were imported into, so they
--                 run through the ordinary pipeline, cards and lever table.
--                 SET NULL on delete: deleting that project disconnects the
--                 reels but keeps the last study readable.
--     listing     every reel the account listing returned, with its counts -
--                 including the ones not imported - so the account baseline
--                 (median views, duration bands, cadence) is over the whole
--                 account and not just the transcribed sample.
--     report      the last "what works / what doesn't" study.
--     style       the style profile rewrites are written to and judged against.
--
-- reel_rewrites: append-only. A rewrite costs a research pass, several LLM
--   drafts and a Jev judging round, so it is kept rather than regenerated on
--   reload.

BEGIN;

ALTER TABLE settings ADD COLUMN IF NOT EXISTS jev_api_key text;

CREATE TABLE IF NOT EXISTS creator_profiles (
  user_id uuid PRIMARY KEY REFERENCES app_users(id) ON DELETE CASCADE,
  username text NOT NULL,
  ig_user_id text,
  project_id bigint REFERENCES projects(id) ON DELETE SET NULL,
  listing jsonb,
  listed_at timestamptz,
  report jsonb,
  style jsonb,
  studied_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reel_rewrites (
  id bigserial PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  reel_id text NOT NULL,
  result jsonb NOT NULL,
  model text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS reel_rewrites_reel_idx
  ON reel_rewrites (project_id, reel_id, created_at DESC);

COMMIT;
