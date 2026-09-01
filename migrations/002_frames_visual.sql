-- Storyboard frames + the vision breakdown that reads them.
--
-- frames: [{"file": "<reel_id>.f00.jpg", "t": 0.25}, ...] - evenly spaced stills
--   pulled locally with ffmpeg right after download. Files live next to the
--   video in data/videos/ and are swept by the same "<reel_id>.*" glob on delete.
-- visual: the AI's frame-grounded read of the video - scenes (label, title,
--   start, end, goal, beat, description), plus hook_type, visual_opening,
--   visual_hook, why_it_works, key_topics, setting, subject, editing,
--   on_screen_text and duration. Written by /api/visual/<rid> or the bulk
--   prepare on the Analysis page; null until one of those has run.

ALTER TABLE reels ADD COLUMN IF NOT EXISTS frames jsonb;
ALTER TABLE reels ADD COLUMN IF NOT EXISTS visual jsonb;
