-- Analysis v2, phase 3: the knowledge layer and the agent's own tables.
--
-- projects.playbook: a short LLM-written "what works in this library, with
--   numbers" regenerated at the end of every formula run. The agent reads it
--   first so a chat doesn't re-derive the library; every claim in it is tied to
--   a lever stat or a formula id. {text, generated_at, run_id, model}.
--
-- agent_threads / agent_messages: in-app chat, per project. Messages keep the
--   tool calls and results (jsonb) so a thread can be replayed and audited -
--   "which reels did it actually look at before saying that".
-- project_notes: durable memory the user confirms ("I post at 7pm IST", "never
--   trend audio"). created_by says whether the user typed it or the agent
--   proposed it. Injected into the agent's system prompt.
-- drafts: scripts the agent wrote, as the structured Script card (hook, beats,
--   cta, on-screen text, caption, levers, expected signals) plus the taxonomy
--   tags predicted for it. posted_reel_id closes the loop later: when an own
--   reel matching a draft is ingested, predicted vs actual can be compared.

BEGIN;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS playbook jsonb;

CREATE TABLE IF NOT EXISTS agent_threads (
  id bigserial PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_threads_project_idx ON agent_threads (project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_messages (
  id bigserial PRIMARY KEY,
  thread_id bigint NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
  content text,
  tool_calls jsonb,      -- assistant: [{id, name, args}]
  tool_results jsonb,    -- tool: [{id, name, result}] (trimmed)
  tokens_in int,
  tokens_out int,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_messages_thread_idx ON agent_messages (thread_id, id);

CREATE TABLE IF NOT EXISTS project_notes (
  id bigserial PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  text text NOT NULL,
  created_by text NOT NULL DEFAULT 'user' CHECK (created_by IN ('user', 'agent')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS project_notes_project_idx ON project_notes (project_id, created_at);

CREATE TABLE IF NOT EXISTS drafts (
  id bigserial PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title text NOT NULL,
  script jsonb NOT NULL,
  predicted_tags jsonb,
  thread_id bigint REFERENCES agent_threads(id) ON DELETE SET NULL,
  posted_reel_id text REFERENCES reels(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS drafts_project_idx ON drafts (project_id, created_at DESC);

COMMIT;
