-- Projects: a per-user workspace that everything else hangs off.
--
-- Before this migration the only scoping dimension was user_id. After it, every
-- content row (reels, formulas, evidence, analysis runs) and the formula
-- category taxonomy itself belong to exactly one project, so a user can keep
-- separate niches side by side without their formulas bleeding into each other.
-- user_id stays on those tables as a denormalized ownership shortcut - it makes
-- the "is this row mine" check a single-table read instead of a join.
--
-- Safe to run once, inside one transaction. Existing data is folded into a
-- "General" project per user; nothing is dropped.

BEGIN;

CREATE TABLE projects (
  id bigserial PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  name text NOT NULL,
  emoji text,
  description text,
  -- Free-text steer injected into this project's AI prompts (Claude-Projects
  -- style). Never overrides the required JSON output shape - see app.py.
  instructions text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX projects_user_name_uidx ON projects (user_id, lower(btrim(name)));
CREATE INDEX projects_user_idx ON projects (user_id, created_at);

-- Every existing account gets one project holding everything it already had.
INSERT INTO projects (user_id, name, emoji, description)
SELECT id, 'General', '📁', 'Everything from before projects existed.'
FROM app_users;

ALTER TABLE reels            ADD COLUMN project_id bigint REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE analysis_runs    ADD COLUMN project_id bigint REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE formulas         ADD COLUMN project_id bigint REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE formula_evidence ADD COLUMN project_id bigint REFERENCES projects(id) ON DELETE CASCADE;

UPDATE reels            t SET project_id = p.id FROM projects p WHERE p.user_id = t.user_id AND p.name = 'General';
UPDATE analysis_runs    t SET project_id = p.id FROM projects p WHERE p.user_id = t.user_id AND p.name = 'General';
UPDATE formulas         t SET project_id = p.id FROM projects p WHERE p.user_id = t.user_id AND p.name = 'General';
UPDATE formula_evidence t SET project_id = p.id FROM projects p WHERE p.user_id = t.user_id AND p.name = 'General';

-- ---- formula_categories: global taxonomy -> per-project taxonomy ----
-- Each project now owns its own category tabs, so the same three defaults are
-- copied into every project rather than shared. formulas.category keeps holding
-- the slug; the FK just widens to the composite (project_id, slug).
ALTER TABLE formulas DROP CONSTRAINT formulas_category_fkey;

ALTER TABLE formula_categories ADD COLUMN project_id bigint REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE formula_categories DROP CONSTRAINT formula_categories_pkey;
DELETE FROM formula_categories;  -- the 3 global rows are re-inserted per project below
ALTER TABLE formula_categories ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE formula_categories ADD PRIMARY KEY (project_id, slug);

INSERT INTO formula_categories (project_id, slug, display_name, sort_order)
SELECT p.id, c.slug, c.display_name, c.sort_order
FROM projects p
CROSS JOIN (VALUES ('hook', 'Hooks', 1), ('script', 'Scripts', 2), ('talking', 'Talking Style', 3))
  AS c(slug, display_name, sort_order);

-- Deleting a category takes its formulas with it - a formula has no meaning
-- outside the category tab it lives in, and the UI warns before deleting.
ALTER TABLE formulas ADD CONSTRAINT formulas_category_fkey
  FOREIGN KEY (project_id, category) REFERENCES formula_categories(project_id, slug) ON DELETE CASCADE;

-- ---- constraints that were user-scoped become project-scoped ----
DROP INDEX formulas_user_category_name_idx;
CREATE UNIQUE INDEX formulas_project_category_name_idx
  ON formulas (project_id, category, lower(btrim(name)));

ALTER TABLE reels            ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE analysis_runs    ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE formulas         ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE formula_evidence ALTER COLUMN project_id SET NOT NULL;

CREATE INDEX reels_project_idx         ON reels (project_id);
CREATE INDEX analysis_runs_project_idx ON analysis_runs (project_id, generated_at DESC);

COMMIT;
