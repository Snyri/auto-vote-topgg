# auto-vote-topgg

Automated daily voting bot for [top.gg](https://top.gg) using nodriver (visible Chrome via Xvfb) + GitHub Actions. Supports multiple Discord accounts and multiple bots.

## Architecture at a Glance

[![auto-vote-topgg repository architecture](assets/repo_infographic.png)](assets/repo_infographic.svg)

> Cookie-first session verification leads into Discord OAuth fallback, terminal CAPTCHA handling, truthful CI outcomes, Telegram reporting, and isolated security controls. Click image for scalable SVG.

## Features

- 🗳️ **External scheduler driven** — Northflank dispatches GitHub Actions from the latest `next-vote` artifact
- ⏱️ **State-aware retry** — success/cooldown and transient failure states all publish a bounded next-attempt timestamp
- 👥 **Multi-account** — vote with multiple Discord tokens and cookie sessions in one run
- 🤖 **Multi-bot** — vote for multiple bots per account
- 🍪 **Cookie-first auth** — injects only top.gg Auth.js cookies, then verifies the session
- 🔐 **OAuth fallback** — uses Discord OAuth when cookies are missing or expired
- ⚡ **Turnstile verification** — dismisses top.gg privacy overlay, then nodriver clicks Cloudflare checkbox during cookie auth, OAuth, pre-vote, post-vote, and verification reload
- 🔒 **Explicit CAPTCHA fallback** — unresolved interactive CAPTCHA is reported and not retried on the same runner
- 🔄 **Browser startup fresh-run retry** — runner-local Chrome startup failure automatically dispatches one fresh-runner retry
- 📨 **Telegram notifications** — chunked per-account reports with privacy-safe account fingerprints
- 🔁 **Scoped retry** — retries transient authentication and bot failures without repeating final results
- 📸 **Failure evidence** — always captures CAPTCHA pages and final auth failures for private Telegram; other error screenshots remain opt-in
- 🚦 **Truthful CI status** — incomplete votes report to Telegram, then fail the workflow
- 🧹 **Auto-cleanup** — keeps the latest 10 completed GitHub Actions runs repository-wide
- 📌 **Reproducible builds** — Python packages and GitHub Actions are pinned to tested immutable versions

## How It Works

```
TOPGG_COOKIES_JSON (same line order as TOKENS)
    ↓ inject top.gg Auth.js cookies
    ↓ inspect vote-page UI + verify /api/auth/session
    ├── vote surface visible → authenticated even if the session endpoint is temporarily blocked
    ├── explicit unauthenticated state → Discord-origin token injection → OAuth Authorize
    └── protection block → one fresh-browser retry, then scheduled backoff
top.gg authenticated
    ↓ navigate to vote page → wait ad → nodriver verify_cf()
    ├── checkbox located by OpenCV → native mouse click → response/page clearance
    ├── unresolved CAPTCHA → captcha_required (no retry this run)
    ├── cooldown text → bounded timestamp → temporary five-minute dispatcher
    └── verified → click Vote → confirm success/cooldown
```

## Setup

### 1. Fork this repository

Fork to your own GitHub account so you can add Secrets and run Actions.

### 2. Get your Discord Token

> [!CAUTION]
> Discord user tokens are sensitive credentials. Never share them.

**Via Network Tab (recommended):**
1. Open [discord.com](https://discord.com) in your browser → press `F12`
2. Go to **Network** tab → filter by **Fetch/XHR**
3. Click any channel or DM to trigger a request
4. Click any request to `discord.com/api/...`
5. In **Request Headers**, find the `Authorization` header → that's your token

**Via Local Storage:**
1. Open [discord.com](https://discord.com) → press `F12`
2. Go to **Application** tab → **Local Storage** → `https://discord.com`
3. Find key `token` → copy the value (without surrounding quotes)

### 3. Configure GitHub Secrets

Go to your repo **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Required | Description |
|--------|:--------:|-------------|
| `TOKENS` | ✅ | Discord user token(s) — one per line for multi-account |
| `BOT_IDS` | ❌ | Bot ID(s) to vote for — one per line. Default: `830530156048285716` |
| `TG_BOT_TOKEN` | ❌ | Telegram bot token (from [@BotFather](https://t.me/BotFather)) |
| `TG_CHAT_ID` | ❌ | Telegram chat/user ID for vote result notifications |
| `SEND_ERROR_SCREENSHOTS` | ❌ | Set to `1` for non-CAPTCHA error/uncertain screenshots; CAPTCHA screenshots are automatic |
| `TOPGG_COOKIES_JSON` | ❌ | Full extension JSON export, one line per account matching `TOKENS` order |

At runtime, workflow copies credential secrets into mode-`0600` temporary files, unsets raw values, and passes only file paths to Python. Python reads and unlinks those files before Chrome starts. `BOT_IDS` and `SEND_ERROR_SCREENSHOTS` are non-credential configuration values and remain ordinary environment variables.

**`TOKENS` multi-account example:**
```
NzI4MjA0NDU4MjcxMjg2NzMy.XXXXXX.YYYYYYYYYYYY
OTQxNjM3NDU4MjcxMDA2NDAz.XXXXXX.ZZZZZZZZZZZZ
```

**`TOPGG_COOKIES_JSON` multi-account format:**

Install [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) from Chrome Web Store first. Cookie export stays local to browser according to extension listing, but exported Auth.js session data remains a sensitive login credential.

1. Login to top.gg using account 1.
2. Open **Get cookies.txt LOCALLY** on a top.gg page.
3. Select export format **JSON**, then click **Copy**.
4. Paste the copied one-line JSON as line 1 in the secret.
5. Repeat using account 2 and paste as line 2.
6. Use `[]` for an account without exported cookies.

```text
[{"domain":".top.gg","name":"__Secure-authjs.session-token","value":"..."}, ...]
[{"domain":".top.gg","name":"__Secure-authjs.session-token","value":"..."}, ...]
[]
```

The script filters full export automatically and injects only cookie names containing `authjs` for `top.gg`. When this secret exists, its line count must exactly match `TOKENS`; use `[]` for any account without cookies. Before injection, every Auth.js cookie is forced to `Secure` and every session-token cookie is forced to `HttpOnly`, even when extension export omits or misreports those flags. Invalid/misaligned exports fail before browser startup without printing cookie values.

> [!CAUTION]
> Auth.js session cookies are login credentials. Store them only in GitHub Secrets; never commit or share them.

**`BOT_IDS` multi-bot example:**
```
830530156048285716
123456789012345678
```

### 4. Enable GitHub Actions

Go to your repo **Actions** tab → click **"I understand my workflows, go ahead and enable them"**.

## Automatic Schedule and Retry

This fork uses the persistent service in `scheduler/` as the scheduler. The service runs on Northflank and dispatches `.github/workflows/vote.yml` with `workflow_dispatch`; there is no GitHub cron in `vote.yml`.

After every run, `vote.py` writes a private one-day `next-vote` artifact containing only a bounded UTC epoch:

```json
{"next_vote_at": 1786334400}
```

Confirmed success normally schedules the next attempt about 12 hours later. Parsed top.gg cooldowns use the reported duration plus the safety buffer. Protection blocks, CAPTCHA states, and other transient failures also receive bounded retry times, so a failed run does not cause the external scheduler to dispatch again every minute.

The Northflank scheduler waits for an active vote workflow instead of dispatching a duplicate, validates schedule timestamps, retries transient GitHub API reads with backoff, waits at least five minutes after a failed run with no usable schedule, and imposes a maximum workflow wait. Required environment values are `GH_TOKEN`, `GH_REPOSITORY`, `GH_REF`, and `GH_WORKFLOW`; optional timing controls are `POLL_SECONDS`, `ERROR_RETRY_SECONDS`, and `MAX_RUN_WAIT_SECONDS`.


### Browser Startup Fresh-Run Retry

Chrome startup may occasionally fail on a GitHub-hosted runner due to transient runner-level issues. When all accounts fail with a browser startup error (exit code/stderr included in report), `vote.py` writes a credential-free marker artifact:

```json
{"reason":"browser_startup_failed"}
```

The `browser-startup-retry` workflow job reads this artifact and dispatches **exactly one** fresh `vote.yml` run on a new runner. The retry run is identified in the Actions UI by `source=browser-startup-retry` and `origin_run_id=<original-run-id>`.

Guards:
- Retry run with `source=browser-startup-retry` never dispatches again.
- Only first attempt (`run_attempt == 1`) may dispatch.
- Marker artifact contains no tokens, cookies, account IDs, bot IDs, or screenshots.
- Original failed run remains a truthful failure; Telegram error report is sent before retry starts.

## Debugging

To enable verbose diagnostic logging locally, set `DEBUG=1`:

```bash
# Windows
set DEBUG=1 && python vote.py

# Linux / macOS
DEBUG=1 python vote.py
```

Non-CAPTCHA error screenshots remain disabled unless `SEND_ERROR_SCREENSHOTS=1` is set, except final top.gg authentication failures which are captured automatically on the last retry.

CAPTCHA/Turnstile challenges are solved first with nodriver `verify_cf()` wherever they appear: top.gg cookie authentication, Discord OAuth, before voting, after clicking `Vote`, and after vote verification reload. top.gg privacy-consent overlays are checked repeatedly after page open/reload, then dismissed before auth probes, every marked click, vote interaction, and solver clicks so they cannot cover the checkbox or `Vote` button. If privacy-modal dismissal fails while the modal is detected, one screenshot plus the dismiss error is sent to Telegram. If solver cannot clear the challenge, the current browser page is captured when possible and sent to the configured Telegram chat immediately after the text report. Final `auth_failed` results also capture the last browser state and send it after the text report. No `SEND_ERROR_SCREENSHOTS` secret is required for CAPTCHA or final auth-failure evidence.

For other GitHub Actions diagnostics, add repository secret `SEND_ERROR_SCREENSHOTS=1`. Error and uncertain states then send screenshots to configured Telegram chat. Keep chat private: screenshots may contain Discord username, avatar, or top.gg account details. Screenshots are never uploaded as GitHub artifacts and local files are deleted after each Telegram delivery attempt.

> [!WARNING]
> Use ephemeral, single-tenant GitHub-hosted runners only. Do not run this project on persistent/shared self-hosted runners: browser processes handle live account credentials and temporary profiles.

Transient authentication/browser failures retry up to 3 times. Protection-blocked authentication uses at most two browser attempts before deferring to the external scheduler. In multi-bot runs, only bots with `error` or `uncertain` results retry; `success`, `cooldown`, and `captcha_required` are final for the current run. Interactive CAPTCHA is intentionally not retried on the same runner/IP. Telegram reports identify accounts using a short SHA-256 fingerprint, never token fragments, and split automatically below Telegram's message limit.

- `success`, `cooldown`: final on the current runner/IP.
- A cooldown with a valid duration schedules an isolated dispatcher instead of sleeping/retrying on the same runner.
- `captcha_required`: final on the current runner/IP.
- `error`, `auth_failed`, `uncertain`: retry up to 3 times.
- `blocked`: one fresh-browser retry, then a scheduled backoff.

`error`, `auth_failed`, `uncertain`, or `captcha_required` sends its report first, then exits non-zero so GitHub Actions shows failure.

## Project Structure

```text
auto-vote-topgg/
├── vote.py                          # Auth, vote, cooldown state, report, browser lifecycle
├── test_vote.py                     # Vote/auth/browser unit and regression tests
├── test_scheduler.py                # Scheduler validation and dispatch regression tests
├── scheduler/
│   ├── Dockerfile                   # Northflank scheduler image
│   └── scheduler.py                 # Artifact-driven GitHub workflow dispatcher
├── audit_dependencies.py            # Stdlib OSV dependency audit
├── requirements.txt                 # Direct Python dependencies
├── requirements.lock                # Linux/Python 3.11 hashes and transitive pins
├── README.md                        # Setup and operating guide
├── SECURITY.md                      # Disclosure and credential policy
├── assets/
│   ├── repo_infographic.png         # README preview
│   └── repo_infographic.svg         # Scalable architecture source
├── .github/
│   ├── CODEOWNERS                   # Sensitive-file ownership
│   ├── dependabot.yml               # Weekly pip/Actions updates
│   └── workflows/
│       ├── security.yml             # Tests, syntax checks, dependency audit
│       └── vote.yml                 # Secret handoff, vote, artifacts, cleanup
└── .gitignore
```

## Requirements

- Python 3.11+
- `nodriver==0.50.3`, `opencv-python-headless==5.0.0.93`, and `requests==2.34.2` as direct dependencies
- Hash-locked Linux x86_64 / CPython 3.11 dependencies in `requirements.lock`
- Google Chrome/Chromium (discovered dynamically by workflow)
- Xvfb on headless Linux runners (installed by workflow)

`opencv-python-headless` is required by nodriver `verify_cf()`: nodriver captures the viewport, matches its bundled Cloudflare checkbox template, then dispatches a native mouse click. The headless package supplies image matching without OpenCV GUI components.

### Local install

`requirements.lock` intentionally targets GitHub's Linux x86_64 / CPython 3.11 runner. For local development on Windows, macOS, or another Python ABI, install reviewed direct pins:

```bash
python -m pip install -r requirements.txt
```

### GitHub Actions install

CI verifies exact Linux wheels with:

```bash
python -m pip install --require-hashes -r requirements.lock
```

Run local checks:

```bash
python -m unittest -v
python -m py_compile vote.py test_vote.py test_scheduler.py audit_dependencies.py scheduler/scheduler.py
python -m pip check
python audit_dependencies.py requirements.lock
```

Dependabot checks pip and GitHub Actions weekly. Regenerate lock by downloading CPython 3.11 Linux x86_64 wheels for `requirements.txt`, recording exact transitive versions, and adding each wheel SHA-256. Verify resulting install on GitHub Actions before merging.

`master` accepts changes through pull requests. Required `test` and `dependency-audit` checks must pass; branches must be current, conversations resolved, and history linear. Admins follow same policy. Force pushes and branch deletion are blocked. Human approvals remain `0` while repository has only one trusted collaborator.

## ⚠️ Disclaimer

This project automates interactions using Discord user tokens. Using self-bots violates [Discord's Terms of Service](https://discord.com/terms). Use at your own risk. The author is not responsible for any account bans or other consequences.
