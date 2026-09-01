---
updated: 2026-08-21
next_review: 2026-09-01
---

# Instagram algorithm brief

Maintained by the monthly research agent — see `instagram-algorithm.agent.md` for how this
file is produced and what may go in it. The app reads this file at request time and feeds
the sections below into its formula analysis, so treat every line as something the AI will
reason with.

## Current model

Reels ranking is viewer-centric: Instagram's published order puts what the *viewer* recently
did ahead of anything about the creator [S1]. Mosseri narrows the creator-controllable levers
to three — watch time, likes, sends — split by audience type [S2][S3]. Follower count is a
weak proxy for reach; over half of feed content is recommendation, not followed accounts [S5],
and Instagram states small and large accounts have equal opportunity [S12].

## Ranking signals

- Instagram's stated order for Reels, most to least important: (1) your activity — reels you
  liked, saved, reshared, commented on recently; (2) your history of interacting with the
  person who posted; (3) information about the reel — audio track, visuals, popularity;
  (4) information about the poster — follower count, engagement level. [S1]
- Mosseri: "The top three signals that matter most for ranking are watch time, likes and
  sends." Creator-side, that means average watch time, likes per reach, sends per reach. [S2]
- Split by audience: "Likes are slightly more important for connected content, and sends are
  slightly more important for unconnected content." Sends per reach is the lever for reaching
  non-followers; likes track resonance with existing followers. [S2][S3]
- Diagnose with engagement *rate*, not raw reach: "if you want to understand why a post got
  more or less reach, you should focus on your engagement rates." [S3]
- Longer reels are not judged on completion rate alone: "We don't want to penalize longer
  videos, which is why we look at not only the percentage of a video that was watched, but
  also the number of seconds." [S4]
- Views (replays counted) replaced Plays as the headline metric across formats; Mosseri
  directs creators at Views plus Sends per Reach. [S5]

## What suppresses reach

- Made less eligible for recommendation: low-resolution, watermarked, muted, bordered,
  majority-text, or already-posted-to-Instagram reels. [S1]
- Recommendation Guidelines violations (violence, regulated goods) stay on the platform but
  receive reduced distribution across Reels and Explore. [S1]
- Aggregator/repost accounts: from 2026-04-30 Instagram stopped recommending photos and
  carousels from accounts that mostly repost, extending the reels-only system it ran from
  2024. Assessed monthly over a 30-day window; adding value beyond "simply restating or
  referencing" the source avoids the classification, and collab/remix/paid-partnership tools
  preserve attribution. [S6]

## Format guidance

- Reels up to 3 minutes are recommended in discovery surfaces on the same footing as shorter
  ones — the earlier "keep it under 90 seconds" guidance no longer holds. [S7]
- Trial Reels publish to non-followers only. Instagram credits them with an 80% increase in
  reels reach among non-followers, and since 2026-04-02 they can be scheduled. [S8]
- Optional "AI creator" profile label shipped 2026-05-04 — voluntary, encouraged for accounts
  frequently posting gen-AI content. No distribution consequence stated either way. [S9]
- "Your Algorithm" lets viewers add or remove topics driving their recommendations, unified
  across Feed, Reels and Explore since 2026-06-10 — a reel's topic legibility now has a
  direct user-facing dial on it. [S10][S11]
- Directional only, no stated ranking effect: Mosseri expects feeds to "fill up with
  synthetic everything" and suggests raw, unflattering imagery is how creators prove they are
  real. Platform direction, not a mechanic. [S13]

## Community signal

- **Reddit not reachable this run.** Blocked for WebFetch and by policy in the browser pane;
  searches mentioning Reddit returned only SEO blogs and stat aggregators, no thread content.
  Nothing recorded rather than passing off a secondhand summary as Reddit.

## Open questions

- Four widely-circulated 2026 claims could **not** be corroborated by any Tier 1 or Tier 2
  source and are not treated as true here: saves promoted to second-highest signal; DM sends
  carrying "3–5x" the weight of likes; an "audition system" non-follower test pool introduced
  in Q1 2026; reels extended to 20 minutes. Each traces only to SEO/growth blogs.
- The published ranking order [S1] is dated 2023-05-31 and unrefreshed, while Mosseri's
  2025–2026 remarks emphasise different metrics. Unclear whether the order changed or the two
  describe different layers.
- No distribution-affecting announcement found for July or August 2026.

## Superseded

- _(nothing yet — this is the first populated run)_

## Sources

- [S1] Instagram — Instagram Ranking Explained — https://about.instagram.com/blog/announcements/instagram-ranking-explained (Tier 1, 2023-05-31)
- [S2] Social Media Today — Instagram Shares Algorithm Insights To Inform Strategy — https://www.socialmediatoday.com/news/instagram-shares-algorithm-insights-2025/738034/ (Tier 2, 2025-01-22)
- [S3] Social Media Today — Instagram engagement rates provide insight into reach — https://www.socialmediatoday.com/news/instagram-engagement-rates-provide-insight-into-reach/821170/ (Tier 2, 2026-05-26)
- [S4] Social Media Today — Instagram Explains How Its Algorithm Weighs Time Spent Watching Longer Clips — https://www.socialmediatoday.com/news/instagram-longer-video-watch-time-versus-completion-rate/740916/ (Tier 2, 2025-02-25)
- [S5] Social Media Today — Instagram Updates Metrics to Focus Creators on Views — https://www.socialmediatoday.com/news/instagram-updates-metrics-to-focus-creators-on-views/723645/ (Tier 2, 2024-08-07)
- [S6] Social Media Today — Instagram updates algorithm to benefit original creators — https://www.socialmediatoday.com/news/instagram-updates-algorithm-to-benefit-original-creators/819016/ (Tier 2, 2026-04-30)
- [S7] Social Media Today — Instagram Says It Will Recommend Longer Reels in Explore — https://www.socialmediatoday.com/news/instagram-will-recommend-longer-3-minute-reels/737913/ (Tier 2, 2025-01-21)
- [S8] Social Media Today — Instagram allows creators to schedule Trial Reels — https://www.socialmediatoday.com/news/instagram-allows-creators-to-schedule-trial-reels/816549/ (Tier 2, 2026-04-02)
- [S9] Social Media Today — Instagram adds AI Creator labels — https://www.socialmediatoday.com/news/instagram-adds-ai-creator-labels/819267/ (Tier 2, 2026-05-04)
- [S10] Instagram — Control Your Instagram Reels Algorithm — https://about.instagram.com/blog/announcements/reels-algorithm-control (Tier 1, 2025-12-10)
- [S11] Social Media Today — Instagram extends Your Algorithm to the main feed — https://www.socialmediatoday.com/news/instagram-extends-your-algorithm-to-the-main-feed/822576/ (Tier 2, 2026-06-10)
- [S12] Instagram — Finding success on reels in 2025 — https://creators.instagram.com/blog/the-latest-with-instagram (Tier 1, 2025-01-21)
- [S13] Engadget — Instagram chief: AI is so ubiquitous 'it will be more practical to fingerprint real media than fake media' — https://www.engadget.com/social-media/instagram-chief-ai-is-so-ubiquitous-it-will-be-more-practical-to-fingerprint-real-media-than-fake-media-202620080.html (Tier 2, 2025-12-31)

## Change log

- 2026-08-21 — First research run; 13 sources opened (3 Tier 1, 10 Tier 2). Key items:
  sends-per-reach as the non-follower lever, the 2026-04-30 aggregator demotion extending to
  photos/carousels, Trial Reels scheduling, Your Algorithm going cross-surface. Reddit
  unreachable — no Tier 3 signal. Four widely-repeated 2026 claims failed corroboration and
  were quarantined in Open questions.
- 2026-08-21 — File created, empty. Awaiting first research run.
