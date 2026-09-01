-- Agent web research: a per-account search provider + key, like the AI key.
-- 'tavily' | 'langsearch' | 'brave'; blank provider = use the platform/env key if
-- any (TAVILY_API_KEY / LANGSEARCH_API_KEY / BRAVE_API_KEY), else only OpenAI's
-- hosted web_search (available automatically on the Responses API path).
ALTER TABLE settings ADD COLUMN IF NOT EXISTS search_provider text;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS search_api_key text;
