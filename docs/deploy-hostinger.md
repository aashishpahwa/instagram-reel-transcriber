# Deploying for a team (Hostinger VPS, Docker Manager)

One shared server, one shared Neon database, everyone signs up for their own
account. No GPU on the server: transcription runs on Groq's hosted Whisper, so
a Groq key is required in the cloud (see step 3).

## How the pieces fit

- **GitHub → image.** Every push to `master` runs
  [`.github/workflows/docker-image.yml`](../.github/workflows/docker-image.yml),
  which builds the [`Dockerfile`](../Dockerfile) and publishes
  `ghcr.io/aashishpahwa/instagram-reel-transcriber:latest`.
- **Hostinger → container.** Docker Manager reads
  [`docker-compose.yaml`](../docker-compose.yaml) from the repo's `master`
  branch (it fetches that one file, it does not clone), pulls the image above,
  and injects whatever you typed into its *Environment variables* box.
- **Traefik → HTTPS.** The "Ubuntu 24.04 with Docker and Traefik" VPS template
  already runs Traefik with Let's Encrypt. The compose file carries the labels;
  you only supply the hostname (`APP_HOST`).
- **Neon → data + accounts.** Same `DATABASE_URL` and Neon Auth service as local
  dev. Sign-up is open self-serve on the login screen.

## First deploy

1. **Check the image built.** Open the repo's *Actions* tab and wait for
   "Build and publish Docker image" on `master` to go green (about 2-3 min).
   If the VPS later fails to pull the image ("denied" / "unauthorized"), the
   package is still private: go to the package page linked from the repo's
   right-hand *Packages* box → *Package settings* → *Change visibility* →
   *Public*. Do this once.

2. **Create the project in Docker Manager.** hPanel → VPS → *Docker Manager* →
   *Create project* → *Compose from URL*:
   - URL: `https://github.com/aashishpahwa/instagram-reel-transcriber`
   - Project name: `reel-transcriber`
   - Environment variables (one `KEY=value` per line):

   | Variable | Required | Value |
   |---|---|---|
   | `APP_HOST` | yes | Hostname for HTTPS. The VPS's own `srvNNNNNNN.hstgr.cloud` name works immediately; a custom domain works once its A record points at the VPS IP. |
   | `DATABASE_URL` | yes | Neon connection string (Neon console → project → *Connect*). Use the pooled string if offered. |
   | `NEON_AUTH_BASE_URL` | yes | Neon console → *Auth* → the service URL (`https://ep-…neon.tech`). |
   | `PLATFORM_GROQ_API_KEY` | yes, in practice | Transcribes every reel via Groq's `whisper-large-v3`. Without it, each user must add their own Groq key in Settings before anything transcribes. |
   | `PLATFORM_API_KEY` | recommended | OpenAI-compatible key used for the AI breakdown, formulas, agent and Rate my script by anyone who hasn't set their own key in Settings. Billed to you. |
   | `YTDLP_COOKIES_CONTENT` | recommended | A Netscape `cookies.txt` export from a **throwaway** Instagram account, pasted raw or base64-encoded (`base64 -w0 cookies.txt`). Datacenter IPs get login-walled far more than home connections, and view counts need a session regardless. |
   | `INSTAGRAM_ACCOUNT_ID` | optional | Pin the app to one Instagram account's `ds_user_id`. |
   | `GOOD_VIEW_THRESHOLD`, `AMAZING_VIEW_THRESHOLD`, `REACH_*_MULTIPLE` | optional | Performance tiers; see [`.env.example`](../.env.example). |
   | `TAVILY_API_KEY` / `LANGSEARCH_API_KEY` / `BRAVE_API_KEY` | optional | Web search for the agent. |

   Click *Deploy*. The first start pulls the image (~400 MB) and takes a minute.
   The compose file refuses to start without `DATABASE_URL` and
   `NEON_AUTH_BASE_URL`, so a missing one shows up as a deploy error, not a
   broken site.

3. **Trust the domain in Neon Auth.** Neon console → project → *Auth* →
   *Configuration* → *Domains* → add `https://<APP_HOST>` (protocol, no
   trailing slash). Until this is done, sign-in from the deployed URL is
   rejected. `http://localhost:5151` stays listed for local dev.

4. **Open `https://<APP_HOST>`**, use the *Sign up* tab, and create a project.
   That's the same flow you'll send the team.

## What to send the team

> Go to `https://<APP_HOST>`, click **Sign up**, then create a project.
> Paste a reel URL to start. If you have your own OpenAI/Anthropic key, add it
> under Settings; otherwise the shared one is used.

Each account's reels, projects, formulas, ideas and agent threads are private to
that account. Platform keys (`PLATFORM_*`) are shared and billed to whoever owns
them; there is no per-account spend cap yet.

## Shipping an update

1. Merge to `master` and push. Actions rebuilds the image.
2. Docker Manager → project → *Update* (or *Restart*). `pull_policy: always`
   fetches the new `latest`; the data volume (`reel-data`) is kept.

Schema changes: apply the new `migrations/NNN_*.sql` to Neon (SQL editor or
`psql`) **before** redeploying, exactly as for local dev.

## Operating notes

- **One process, many threads.** The container runs gunicorn with a single
  worker on purpose: the download/transcribe/analysis queues and the live job
  table are in-memory. Don't scale it to multiple workers or replicas.
- **Local Whisper is not in the image.** `openai-whisper` and `torch` are
  omitted from [`requirements-server.txt`](../requirements-server.txt); the app
  detects that on the first reel and switches to Groq for the life of the
  process. Rebuild from `requirements.txt` on a GPU box if you want it back.
- **Logs:** Docker Manager → project → container → *Logs*. Application
  messages are prefixed (`[transcribe_audio]`, `[reel stats]`, `[agent]`).
- **Disk:** every reel's MP4, thumbnail and frames stay in the `reel-data`
  volume. KVM 2 has 100 GB; delete old projects from inside the app when it
  matters.
- **No Traefik on the server?** Remove the `labels:` block in
  `docker-compose.yaml`, uncomment `ports:`, and use `http://<VPS IP>:5151`.
  Add that origin to Neon Auth's trusted domains instead.

## Building on the VPS instead of pulling from GHCR

If you'd rather not depend on the GitHub-built image, paste this into Docker
Manager's *Manual compose* editor (it needs `git` on the VPS for the remote
build context; `apt install git` if it's missing). Everything else is identical.

```yaml
services:
  app:
    build:
      context: https://github.com/aashishpahwa/instagram-reel-transcriber.git#master
    # ...copy environment, volumes, healthcheck and labels from docker-compose.yaml
volumes:
  reel-data:
```
