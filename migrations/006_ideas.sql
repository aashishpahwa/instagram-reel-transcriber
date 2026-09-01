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
