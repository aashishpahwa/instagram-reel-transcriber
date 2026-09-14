-- Reel Transcriber: Neon schema (current state).
--
-- Applied via the Neon console/MCP, not by the app at boot. Incremental changes
-- live in migrations/ - this file is the flattened result, for reading and for
-- standing up a fresh database.
--
-- Two scoping dimensions, both enforced in the queries themselves:
--   user_id    - which account owns the row (Neon Auth user, mirrored into app_users)
--   project_id - which of that account's projects the row belongs to
-- Every content row carries both. user_id is denormalized onto the child tables
-- so an ownership check never needs a join.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Neon Auth users, mirrored on first authenticated request (see auth.ensure_app_user).
CREATE TABLE app_users (
  id uuid PRIMARY KEY,
  email text,
  created_at timestamptz NOT NULL DEFAULT now(),
  transcription_count int NOT NULL DEFAULT 0,
  transcription_quota int,          -- unused hook for a future per-account spend cap
  rate_limited_until timestamptz
);

-- A workspace. Everything a user creates belongs to exactly one project, so two
-- niches can be tracked side by side without their formulas bleeding together.
CREATE TABLE projects (
  id bigserial PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  name text NOT NULL,
  emoji text,
  description text,
  -- Free-text steer injected into this project's AI prompts (Claude-Projects
  -- style). Never overrides the required JSON output shape - see app.py.
  instructions text,
  -- The account this project is "about", if any: reels from it get source='own'.
  own_username text,
  -- How the outcome band is decided for analysis: 'views' (absolute thresholds)
  -- or 'reach' (views / follower count bands).
  outcome_mode text NOT NULL DEFAULT 'views' CHECK (outcome_mode IN ('views', 'reach')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX projects_user_name_uidx ON projects (user_id, lower(btrim(name)));
CREATE INDEX projects_user_idx ON projects (user_id, created_at);

-- id is "<project_id>_<instagram shortcode>": project-prefixed so the same reel
-- can sit in two projects as two independent rows instead of the second
-- overwriting the first, and so the media files (<id>.mp4/.jpg) don't collide.
CREATE TABLE reels (
  id text PRIMARY KEY,
  url text NOT NULL,
  status text NOT NULL,
  transcript text,
  segments jsonb,
  video_file text,
  thumb_file text,
  -- [{"file": "<reel_id>.f00.jpg", "t": 0.25}, ...] - evenly spaced stills pulled
  -- locally with ffmpeg after download. Free, so every reel gets them.
  frames jsonb,
  -- The AI's frame-grounded read of the video: scenes (label, title, start, end,
  -- goal, beat, description) plus hook_type, visual_opening, visual_hook,
  -- why_it_works, key_topics, setting, subject, editing, on_screen_text. Costs
  -- API tokens, so it's opt-in per reel - null until it has been run.
  visual jsonb,
  created_at timestamptz,
  meta jsonb,
  analysis jsonb,
  error text,
  user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  -- 'own' when meta.username matches the project's own_username, else 'reference'.
  source text NOT NULL DEFAULT 'reference' CHECK (source IN ('own', 'reference')),
  caption text,          -- from the media response; the text hook + topic signal
  hashtags text[],       -- parsed off the caption, lowercased, in order
  tags jsonb,            -- phase-2 Reel Card: structured taxonomy tags
  diagnosis jsonb,       -- phase-2 Reel Card: per-reel diagnosis
  insights jsonb         -- hand-entered IG Insights for own reels
);
CREATE INDEX reels_project_idx ON reels (project_id);
CREATE INDEX reels_tags_gin ON reels USING gin (tags);

-- One row per Instagram account a reel has been seen from. follower_count is
-- the denominator of reach_multiple (views / followers) and is snapshotted into
-- reels.meta.follower_count when a reel's stats are fetched. Refreshed when older
-- than CREATOR_REFRESH_DAYS (app.py).
CREATE TABLE creators (
  username text PRIMARY KEY,
  ig_user_id text,
  full_name text,
  follower_count int,
  following_count int,
  media_count int,
  is_verified boolean,
  fetched_at timestamptz NOT NULL DEFAULT now()
);

-- Per-account AI provider config. Projects share it; the API key is an account
-- concern, not a per-workspace one.
CREATE TABLE settings (
  user_id uuid PRIMARY KEY REFERENCES app_users(id) ON DELETE CASCADE,
  provider text NOT NULL DEFAULT 'openai',
  base_url text NOT NULL DEFAULT 'https://api.openai.com/v1',
  model text NOT NULL DEFAULT 'gpt-4o-mini',
  api_key text,
  groq_api_key text,  -- Groq-hosted Whisper, the cloud transcription fallback when local Whisper/GPU isn't available
  -- Agent web research: 'tavily' | 'langsearch' | 'brave' + key (blank = platform env key, else hosted search only)
  search_provider text,
  search_api_key text
);

CREATE TABLE analysis_runs (
  id bigserial PRIMARY KEY,
  generated_at timestamptz NOT NULL DEFAULT now(),
  amazing_count int NOT NULL DEFAULT 0,
  good_count int NOT NULL DEFAULT 0,
  low_count int NOT NULL,
  summary text,
  caveats text,
  user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX analysis_runs_project_idx ON analysis_runs (project_id, generated_at DESC);

-- The formula tabs, owned per project: each project defines its own taxonomy.
-- New projects are seeded with hook/script/talking (DEFAULT_CATEGORIES in app.py).
CREATE TABLE formula_categories (
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  slug text NOT NULL,
  display_name text NOT NULL,
  sort_order int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, slug)
);

CREATE TABLE formulas (
  id bigserial PRIMARY KEY,
  category text NOT NULL,
  name text NOT NULL,
  description text NOT NULL,
  reason text,
  rating smallint CHECK (rating BETWEEN 1 AND 5),
  status text NOT NULL CHECK (status IN ('working', 'not_working')),
  times_seen int NOT NULL DEFAULT 1,
  last_run_id bigint REFERENCES analysis_runs(id),
  equation jsonb,
  template text,
  levers jsonb,          -- phase 3: the taxonomy values the formula is made of
  effect text CHECK (effect IN ('positive', 'neutral', 'negative')),
  lift numeric,          -- phase 3: median outcome with the lever / project median
  taxonomy_v int,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  -- Deleting a category takes its formulas with it: a formula has no meaning
  -- outside the tab it lives in. The UI states the count before deleting.
  CONSTRAINT formulas_category_fkey FOREIGN KEY (project_id, category)
    REFERENCES formula_categories(project_id, slug) ON DELETE CASCADE
);
CREATE UNIQUE INDEX formulas_project_category_name_idx
  ON formulas (project_id, category, lower(btrim(name)));
CREATE INDEX formulas_name_trgm_idx ON formulas USING gin (name gin_trgm_ops);

CREATE TABLE formula_evidence (
  formula_id bigint NOT NULL REFERENCES formulas(id) ON DELETE CASCADE,
  reel_id text NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
  added_at timestamptz NOT NULL DEFAULT now(),
  breakdown jsonb,
  user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  PRIMARY KEY (formula_id, reel_id)
);

-- Creative Corner: a per-project brainstorming board of ideas and scripts.
-- Cards move idea -> scripting -> ready -> posted; ord is a float so a drag
-- lands between two neighbours without renumbering the column.
CREATE TABLE ideas (
  id bigserial PRIMARY KEY,
  title text NOT NULL DEFAULT '',
  notes text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'idea' CHECK (status IN ('idea', 'scripting', 'ready', 'posted')),
  color text NOT NULL DEFAULT 'yellow',
  tags jsonb NOT NULL DEFAULT '[]',
  -- {hook, promise, validation, cta, full} - the user's own script, structured
  -- the same way the AI breakdown splits reference reels.
  script jsonb NOT NULL DEFAULT '{}',
  -- reel ids linked as inspiration. Not a FK: a deleted reel just renders as a
  -- dead chip rather than silently vanishing from the idea.
  refs jsonb NOT NULL DEFAULT '[]',
  ord double precision NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX ideas_project_idx ON ideas (project_id, status, ord);

-- Rate my script: immutable snapshots. Every successful run is inserted, even
-- when the text is unchanged; previous_rating_id links an edited re-rating to
-- the result the user was responding to.
CREATE TABLE script_ratings (
  id bigserial PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  previous_rating_id bigint REFERENCES script_ratings(id) ON DELETE SET NULL,
  title text NOT NULL DEFAULT '',
  goal text NOT NULL DEFAULT '',
  script text NOT NULL,
  script_hash text NOT NULL,
  rating jsonb NOT NULL,
  model text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX script_ratings_project_created_idx
  ON script_ratings (project_id, created_at DESC, id DESC);
CREATE INDEX script_ratings_user_project_idx
  ON script_ratings (user_id, project_id);
CREATE INDEX script_ratings_previous_idx
  ON script_ratings (previous_rating_id)
  WHERE previous_rating_id IS NOT NULL;
