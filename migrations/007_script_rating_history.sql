-- Durable, append-only history for Rate my script.
--
-- Every successful rating stores the exact script/title/goal it scored. A
-- re-rating may point at the prior snapshot so the model and UI can compare the
-- user's edit without overwriting either version.

BEGIN;

CREATE TABLE IF NOT EXISTS script_ratings (
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

CREATE INDEX IF NOT EXISTS script_ratings_project_created_idx
  ON script_ratings (project_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS script_ratings_user_project_idx
  ON script_ratings (user_id, project_id);
CREATE INDEX IF NOT EXISTS script_ratings_previous_idx
  ON script_ratings (previous_rating_id)
  WHERE previous_rating_id IS NOT NULL;

COMMIT;
