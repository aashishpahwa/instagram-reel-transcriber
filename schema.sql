-- Reel Transcriber: Neon schema
-- Applied once via Neon MCP; migrate_to_neon.py assumes this already exists.

CREATE TABLE reels (
  id text PRIMARY KEY,
  url text NOT NULL,
  status text NOT NULL,
  transcript text,
  segments jsonb,
  video_file text,
  thumb_file text,
  created_at timestamptz,
  meta jsonb,
  analysis jsonb,
  error text
);

CREATE TABLE settings (
  id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  provider text NOT NULL DEFAULT 'openai',
  base_url text NOT NULL DEFAULT 'https://api.openai.com/v1',
  model text NOT NULL DEFAULT 'gpt-4o-mini',
  api_key text
);
INSERT INTO settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

CREATE TABLE analysis_runs (
  id bigserial PRIMARY KEY,
  generated_at timestamptz NOT NULL DEFAULT now(),
  high_count int NOT NULL,
  low_count int NOT NULL,
  mid_count int NOT NULL,
  summary text,
  caveats text
);

CREATE TABLE formula_categories (
  slug text PRIMARY KEY,
  display_name text NOT NULL,
  sort_order int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO formula_categories (slug, display_name, sort_order) VALUES
  ('hook', 'Hooks', 1),
  ('script', 'Scripts', 2),
  ('talking', 'Talking Style', 3);

CREATE TABLE formulas (
  id bigserial PRIMARY KEY,
  category text NOT NULL REFERENCES formula_categories(slug),
  name text NOT NULL,
  description text NOT NULL,
  reason text,
  rating smallint CHECK (rating BETWEEN 1 AND 5),
  status text NOT NULL CHECK (status IN ('working', 'not_working')),
  times_seen int NOT NULL DEFAULT 1,
  last_run_id bigint REFERENCES analysis_runs(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX formulas_category_name_uidx ON formulas (category, lower(btrim(name)));

CREATE TABLE formula_evidence (
  formula_id bigint NOT NULL REFERENCES formulas(id) ON DELETE CASCADE,
  reel_id text NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
  added_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (formula_id, reel_id)
);

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX formulas_name_trgm_idx ON formulas USING gin (name gin_trgm_ops);
