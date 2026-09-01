# Agent brief — monthly Instagram algorithm review

You are updating a single living file that the Reel Transcriber app reads at runtime:
**`research/instagram-algorithm.md`**.

That file is injected into the app's formula-analysis prompt, so what you write becomes
context the AI reasons with when it decides which content patterns are working. Wrong or
invented material there quietly corrupts every analysis run downstream. Accuracy beats
completeness, and "nothing changed this month" is a valid, useful answer.

Run on the **1st of each month**. Takes 15–30 minutes.

---

## 1. What you are looking for

Changes since the `updated:` date in the existing file that affect **how short-form video
(Reels) gets distributed**:

- Ranking signals Instagram states or that Mosseri describes — and any change in their
  stated priority order
- What gets suppressed or made less eligible for recommendation (watermarks, reposts,
  aggregator behaviour, resolution, borders, AI-generated content, etc.)
- Format/eligibility rules: length limits, aspect ratio, audio, captions, trial reels,
  originality rules
- Metric definitions changing (e.g. views replacing plays) — these change what creators
  can even measure
- Anything that shifts *what kind of content* gets reach: authenticity pushes, AI-content
  labelling, recommendation-guideline changes

Explicitly **out of scope**: monetization, ads products, Threads, shopping, safety/policy
news that doesn't touch distribution, and generic "post consistently" advice.

## 2. Sources, in tiers

Label every claim with the tier it came from. Never promote a lower tier by wording it
like a fact.

**Tier 1 — primary.** Instagram/Meta themselves.
- `about.instagram.com/blog` (especially "Instagram Ranking Explained")
- `creators.instagram.com`, `business.instagram.com`, `help.instagram.com`
- `about.fb.com` newsroom
- Adam Mosseri's own posts/videos, when quoted verbatim by a Tier 2 outlet

**Tier 2 — reputable trade press** that reports on primary announcements.
- Social Media Today, TechCrunch, Engadget, Search Engine Land, Adweek, CNBC

**Tier 3 — community observation.** Reddit (r/Instagram, r/InstagramMarketing,
r/NewTubers, r/socialmedia), creator forums, Discord summaries.
- This tier is *anecdote about lived experience*, never fact about mechanics. It is
  valuable for one thing: telling you what creators are actually experiencing that the
  official line doesn't explain (e.g. a reach collapse nobody announced).
- Write it as "creators report X", never "Instagram now does X".

**Never cite**: SEO listicles, "growth hack" blogs, SMM panel sites, AI-spun content
farms, or anything that cites only itself. If a page's only source is another listicle,
drop it.

### Reddit access

In the Claude Code environment this file was written for, `reddit.com` and `old.reddit.com`
are blocked for both WebFetch and WebSearch `allowed_domains` — a direct fetch fails with
"unable to fetch", and the browser pane refuses them by policy. **Check for a working path
before assuming there isn't one**, in this order:

1. **A Reddit-capable CLI.** Check whether one is installed:
   ```bash
   command -v agent-reach; command -v rdt-cli
   ```
   [Agent Reach](https://github.com/Panniantong/Agent-Reach) is the one this project was
   evaluated against — a Python CLI that reads Reddit (plus X, YouTube, HN) using the
   machine's own logged-in browser session, no API key. If `agent-reach` is on PATH, use it
   to pull the last month of r/Instagram, r/InstagramMarketing, r/socialmedia and
   r/NewTubers, then run `agent-reach doctor` first if a call fails — its Reddit backend
   depends on a live browser session that can expire.
2. **A Reddit MCP server**, if one is connected to the session.
3. **Browser tools** (Claude in Chrome), if this session has them and the policy allows the
   domain.
4. `WebSearch` with plain-text queries that *mention* reddit (no domain filter), e.g.
   `reddit instagram algorithm reels reach dropped August 2026`. This mostly returns SEO
   blogs rather than real threads — treat anything from it as Tier 2 at best, and never
   label it as Reddit.
5. If none of those work, write **"Reddit not reachable this run"** in the Community section
   and move on.

Do not substitute a blog's summary of Reddit and present it as Reddit. Fabricating community
sentiment is the single worst failure mode here — an empty section is strictly better, and
the app renders it honestly either way.

When you do get real threads, what's worth recording is the *unexplained*: a reach change
creators are all describing that no announcement accounts for, a format that suddenly stops
working, a pattern of shadowban reports. Ordinary complaining is not a signal.

## 3. Method

1. **Read the existing `research/instagram-algorithm.md` first.** You are amending it, not
   rewriting it. Note its `updated:` date — that is your search window.
2. Check Tier 1 directly. Fetch the ranking-explained page and the announcements blog and
   diff against what the file already says.
3. Search Tier 2 for the window, month by month if the gap is long. Prefer articles that
   quote a primary source; open them and read the quote rather than trusting a summary.
4. Do the Reddit/community pass per §2.
5. For every candidate claim ask: *is this dated, attributable, and about distribution?*
   If not, drop it.
6. Corroborate anything surprising with a second independent source before it goes in as
   fact. One outlet's paraphrase is not confirmation.

## 4. Writing the file

Keep the exact section headings below — the app parses on them, and renaming one silently
drops it from the analysis prompt.

- Each bullet ends with a source ref like `[S3]`.
- Every entry in `## Sources` is `- [S3] Publisher — Title — URL (Tier N, YYYY-MM-DD)`.
  The date is the article's date, not the date you read it.
- Keep the whole file under ~700 words of body text. It is prompt context, not an archive.
  Being ruthless about what earns a line is the job.
- Prefer specific and falsifiable ("sends per reach is weighted above likes for
  non-followers") over vague ("engagement matters").

**When something changes:** move the superseded bullet into `## Superseded` with the date
it stopped being true and one clause on what replaced it. Never silently delete — the
history is what lets a future run spot a reversal.

**When nothing changed:** bump `updated:`, add a dated line to `## Change log` saying no
distribution-affecting changes were found, and stop. Do not pad.

**Never**: invent a date, cite a URL you did not open, restate a rumour as confirmed, or
let a number through without a source. If you are unsure, put it in
`## Open questions` instead — that section exists so uncertainty has somewhere to live
other than the confident sections.

## 5. Finishing

1. Update `updated:` to today and `next_review:` to the 1st of next month.
2. Add a `## Change log` line: date, one sentence on what changed, how many sources.
3. Re-read your own Ranking signals section and ask: *would a creator make a different
   video because of this?* If not, it is filler — cut it.
4. Report back: what changed, what you could not verify, and which sources were
   unreachable.

The app picks the file up on its next request — nothing to restart, no database to touch.
