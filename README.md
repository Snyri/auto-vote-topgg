# auto-vote-topgg

Automated daily voting bot for [top.gg](https://top.gg) using nodriver (visible Chrome via Xvfb) + GitHub Actions. Supports multiple Discord accounts and multiple bots.

## Architecture at a Glance

```mermaid
flowchart LR
    Scheduler[Northflank scheduler] -->|workflow dispatch| Actions[GitHub Actions]
    Actions --> Browser[Ephemeral browser]
    Browser --> Auth[Cookies and verified application state]
    Auth --> Vote[Vote and observed acknowledgement]
    Vote --> Result[Success, cooldown, or explicit failure]
    Result --> Schedule[Validated next-vote artifact]
    Schedule --> Scheduler
    Result --> Report[Private Telegram report]
    Result --> CI[Workflow exit status]
```

The scheduler follows observed eligibility and bounded retry deadlines. A disappearing challenge widget alone never proves authentication or a completed vote.

## Features

- 🗳️ **External scheduler driven** — Northflank dispatches GitHub Actions from the latest `next-vote` artifact
- ⏱️ **State-aware retry** — success/cooldown and transient failure states all publish a bounded next-attempt timestamp
- 👥 **Multi-account** — vote with multiple Discord tokens and cookie sessions in one run
- 🤖 **Multi-bot** — vote for multiple bots per account
- 🍪 **Cookie-first auth** — injects only top.gg Auth.js cookies, then verifies the session
- 🔐 **OAuth fallback** — uses Discord OAuth when cookies are missing or expired
- ⚡ **Turnstile verification** — dismisses top.gg privacy overlay, then a scored, stable target receives native Cloudflare checkbox input during cookie auth, OAuth, pre-vote, post-vote, and verification reload
- 🔒 **Explicit CAPTCHA fallback** — unresolved interactive CAPTCHA is reported and not retried on the same runner
- 🔄 **Fresh-run recovery** — every failed master run dispatches a fresh GitHub runner, with no recovery-chain limit
- 📨 **Telegram notifications** — chunked per-account reports with privacy-safe account fingerprints
- 🔁 **Scoped retry** — retries transient authentication and bot failures without repeating final results
- 📸 **Failure evidence** — always captures CAPTCHA pages and final auth failures for private Telegram; other error screenshots remain opt-in
- 🚦 **Truthful CI status** — incomplete votes report to Telegram, then fail the workflow
- 🧹 **Auto-cleanup** — keeps the latest 30 completed vote-workflow runs for better failure forensics
- 📌 **Reproducible builds** — Python packages and GitHub Actions are pinned to tested immutable versions

## How It Works

```
TOPGG_COOKIES_JSON (same line order as TOKENS)
    ↓ inject top.gg Auth.js cookies
    ↓ inspect vote-page UI + verify /api/auth/session
    ├── vote surface visible → authenticated even if the session endpoint is temporarily blocked
    ├── explicit unauthenticated state → Discord-origin token injection → OAuth Authorize
    └── protection block → up to five browser attempts, then a fresh workflow run
top.gg authenticated
    ↓ navigate to vote page → wait ad → verified Cloudflare checkbox interaction
    ├── verified native checkbox input → observe response/page state; input alone is not clearance
    ├── unresolved CAPTCHA → captcha_required (no retry this run)
    ├── cooldown text → bounded timestamp → scheduler waits until that timestamp
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
| `BOT_IDS` | ✅ | Bot ID(s) to vote for — one per line |
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

Confirmed success normally schedules the next attempt about 12 hours later. Parsed top.gg cooldowns use the reported duration plus the safety buffer. Protection blocks, CAPTCHA states, and other transient failures also receive bounded retry times, so the external scheduler retains a next-attempt timestamp. Independently, failed master workflow runs now request an immediate fresh run without waiting for that timestamp.

The Northflank scheduler waits for an active vote workflow instead of dispatching a duplicate, validates schedule timestamps, retries transient GitHub API reads with backoff, waits at least five minutes after a failed run with no usable schedule, imposes a maximum workflow wait, and periodically refreshes the latest vote artifact while sleeping. Refreshes never accept an older run ID, and short retries from newer failed runs cannot advance a still-future schedule from the most recent successful run. A final schedule check also runs before dispatch. Required environment values are `GH_TOKEN`, `GH_REPOSITORY`, `GH_REF`, and `GH_WORKFLOW`; optional timing controls are `POLL_SECONDS`, `ERROR_RETRY_SECONDS`, and `MAX_RUN_WAIT_SECONDS`.

Accepted run IDs and deadlines are retained across dispatch cycles. When the run list is stale, the scheduler queries the known run directly. A failed run's longer backoff is also respected: the later of its deadline and a previous successful future deadline wins. `SCHEDULE_REFRESH_SECONDS` controls refresh frequency (default 60 seconds). Changes under `scheduler/` take effect after rebuilding and redeploying the Northflank service; merging this repository alone does not prove that the running service uses the new image.


### Fresh-Run Recovery

Chrome startup may occasionally outlive nodriver's short initial DevTools polling window on a GitHub-hosted runner. The runtime now bounds the initial start call, gives a still-running Chrome process an additional late-attach window, and retries more than the historical five-attempt limit. The workflow also creates a valid D-Bus session when the runner provides `dbus-run-session`, while preserving Xvfb. If all accounts still fail with a browser startup error, `vote.py` writes a credential-free marker artifact:

```json
{"reason":"browser_startup_failed"}
```

The browser-startup and protection-retry artifacts remain diagnostic markers. Fresh-run recovery no longer depends on them: after the vote, verification, and cleanup jobs finish, any failed master run dispatches a new `vote.yml` run with `source=failure-retry` and `origin_run_id=<failed-run-id>`. This also covers setup failures and job timeouts.

There is no delay or maximum number of fresh runs, including after a manually rerun failure. Each new run can try up to five account/browser attempts. The shared concurrency group lets the next run start after the previous run finishes, subject to GitHub runner availability. The original run remains failed. Success stops automatic recovery; cancelling the current run stops this chain (the independent external scheduler remains enabled). The legacy `recovery_depth` input is accepted for compatibility but imposes no limit. GitHub service, API and usage limits still apply, and a dispatch API failure is reported as a failed retry job.

## Debugging

### Vote mouse input after ads

The Vote action waits for an enabled, visible control with stable geometry for
500 ms. It checks that the control is under the pointer (not covered by an ad or
consent layer), reacquires it after page updates, and checks again after hover.
It then sends Chrome mouse press/release events instead of nodriver's JavaScript
`element.click()` shortcut. Logs report whether trusted pointer/click events
reached that specific control; a received click still requires the existing vote
acknowledgement checks before reporting success.

If the control never becomes actionable, the result explicitly states that no
mouse press was sent. Once a press may have been sent, an uncertain outcome does
not trigger another click. `test_vote_pointer.py` exercises this behavior against
local HTML fixtures using Chrome, without contacting top.gg; set `CHROME_BIN` if
Chrome is not on `PATH`.

### Cloudflare 403 diagnosis

The live vote flow also uses `/api/graphql`. Bounded request inspection identifies
single vote mutations and vote-state queries from their actual root fields and
linked arguments. Discord bot IDs and Top.gg's internal entity IDs are kept
distinct; another entity cannot clear the current entity's denial. Aliases,
variable defaults and nested output fragments are supported. Mixed mutations,
unresolved persisted queries and unsupported root fragments remain conservative.
Only a trusted Vote press followed by completed Cloudflare denials for every
tracked same-site write permits another submission. Parsed read-only GraphQL
queries do not veto that rejection; unknown writes still do.

A GraphQL HTTP 200 is inspected for protocol errors and CAPTCHA/authentication
outcomes, including typed results. It never establishes a successful vote by
itself. API readiness needs a fresh completed JSON state response with usable
data and no errors; page acknowledgement or persisted cooldown still establishes
the actual outcome. Public diagnostics contain only fixed categories, never
query text, variables, aliases, tokens or response messages.

The response inspector also observes an unrecognized root field. A single
mutation explicitly targeting the current bot can veto an optimistic acknowledgement
on an application error, but remains an unknown write for resubmission. An
identified vote takes precedence over ancillary mutation errors. Diagnostics
include `graphql_gate` to distinguish missing/oversized payloads, unsupported
documents, unrecognized fields and unresolved targets. The allowlist is not proof
that the live Top.gg schema is covered; inspect these categories on actual runs.

Navigation uses `Page.navigate` on the existing CDP session. In nodriver 0.50.3,
`Tab.get()` attaches a new session, breaking the relationship between enabled
Network observers and subsequent response-body reads. `Page.enable` activates
main-document navigation events. A committed main document gets a new readiness
context; subframes and late responses from the earlier document cannot change it.

Checkbox targeting first searches real enabled controls in provider-owned frames
and closed shadow DOM, checks viewport geometry and hit testing, then reacquires
the stable target after hover. Its appearance need not match an old screenshot.
The conservative image matcher remains a fallback for opaque frames. No checkbox
means no checkbox input; managed verification can clear without a click. A
provider-marked current document also identifies localized interstitials.

The second cookie-session check is passive: it observes late page progress without
repeating the same challenge interaction or session-fetch recovery on that page.
Five account attempts and immediate unlimited fresh-workflow retries remain in
place. Actions also publish an aggregate outcome summary when Telegram is absent.
The `Check recorded vote result` job checks the script's exit status and does not
make an independent request to Top.gg.

Verification disappearance is accepted only on a ready document across two
observations. An empty document during navigation cannot clear a challenge or
trigger a session fetch. A residual managed-challenge title does not block an
otherwise actionable Vote control; human-verification text still does.

A denied session fetch first gets a bounded application-state recheck. If the
challenge exists only in that API response, one ordinary reload of the exact bot
vote page can render verification. This recovery cannot loop or reopen an API,
OAuth callback, or unrelated origin. No response HTML is injected.

Login, Authorize, and consent use native mouse input with visibility, geometry,
hover and event-receipt checks. Authentication still requires the expected
redirect and application state; consent requires the overlay to disappear.
Invalid Auth.js cookies are removed selectively, preserving Cloudflare clearance
and cookies belonging to other origins.

Within one account's five attempts, recoverable failures can reuse a browser
that remains authenticated and usable across two observations. Persistent
protection, lost authentication, disconnected documents and uncertain submissions
replace the profile. Only pending bots retry; completed or unconfirmed
submissions are not clicked again in that run. The profile is closed and deleted
when that account ends, including cancellation; no profile crosses accounts or
workflow runs. Unlimited fresh-workflow recovery remains as configured.

Vote responses also guide recovery. Passive CDP tracking recognizes exact
bot-specific vote API routes and single known vote RPC operations with an
explicit matching bot identifier. It records the actual trusted mouse press
time, rather than treating an arbitrary POST near the click as a submission.
Public diagnostics add `vote_operation` (`vote_submission`, `vote_state`, or
`unrelated`) and a fixed-vocabulary API route template, with identifiers and
unknown segments replaced and query strings removed. Raw URLs, request payloads
and credentials are never retained or printed; bounded RPC input is inspected
only for explicit bot identifiers.

Cloudflare challenges on recognized vote/state operations cause a short
pre-click wait. Elapsed time never clears a denial on the same document: a fresh,
completed JSON vote-state response must establish recovery, without a redirect,
transport failure or conflicting status. A new committed document, bot or browser
starts a separate context. If the app does not repeat its denied read, preflight
can reopen the ordinary vote page once before any input and check its prerequisites
again. A fresh denial still blocks the click; no API URL is opened as a page.
The check runs during control preparation, after hover and immediately before
the native mouse press, so a denial arriving after page preflight still prevents
input. Recovery during hover restarts the control checks; a persistent challenge
returns a protection block before any mouse press. The existing retry then opens
a fresh browser and checks authentication, cooldown and the ad again.

After a trusted click, a completed exact vote request with HTTP 403 and
`cf-mitigated: challenge`/HTML can establish that the submission was rejected.
Only when all tracked submissions have that outcome does the result become
retryable `blocked` with `submission_rejected=true` and `vote_submitted=false`.
This skips unnecessary confirmation reloads and uses the existing five-attempt
fresh-browser recovery. A missing response, redirect, unknown endpoint, mixed
success/denial, unknown same-site write, unfinished request or incomplete tracking keeps the original
uncertain-submission protection. HTTP 200 alone never confirms a vote; page
acknowledgement/cooldown checks still determine success. Unlimited fresh-workflow
recovery remains unchanged.

Passive CDP diagnostics classify failed requests and top.gg API write responses
around the Vote interaction. Logs contain fixed request/phase categories,
HTTP status, content category and safe Cloudflare indicators, without URLs,
bodies, cookie values or authorization headers. Extra response metadata also
captures denials hidden from page JavaScript by CORS. A successful HTTP response
alone never marks a vote as successful.

The retained September 24 runs returned `403`, `text/html`, and `cf-mitigated: challenge` from `/api/auth/session`. This identifies a Cloudflare Challenge Page, not an expired Auth.js cookie. The exact WAF rule and IP reputation are not exposed by those logs. See the [full review and run evidence](docs/audit-2026-09-24.md).

After a detected challenge, authentication waits for recognizable application UI before requesting the session endpoint. Managed challenge titles and DOM containers also count as protection; a transiently missing widget is not logged as verified access. Session requests have a 12-second browser timeout plus a bounded outer wait, so a hanging fetch cannot consume the entire workflow run.

[Cloudflare documents](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/) `cf-mitigated: challenge` as the response marker. Its [supported-browser guidance](https://developers.cloudflare.com/cloudflare-challenges/reference/supported-browsers/) does not support automated browsers for production challenges. These changes reduce avoidable requests and report denial accurately; they cannot guarantee that top.gg will authorize an automated browser. Persistent denial requires resolution with the site operator or normal interactive access.

To enable verbose diagnostic logging locally, set `DEBUG=1`:

```bash
# Windows
set DEBUG=1 && python vote.py

# Linux / macOS
DEBUG=1 python vote.py
```

Non-CAPTCHA error screenshots remain disabled unless `SEND_ERROR_SCREENSHOTS=1` is set, except final top.gg authentication failures which are captured automatically on the last retry.

A Vote click alone is not success. The browser first looks for a **new acknowledgement on the current bot page**: strong success/cooldown text absent before the click, observed in two consecutive checks, with no enabled Vote button, active challenge, login requirement, or explicit error. A retained widget with a response is not by itself an active challenge; visible challenge gates and challenge text still veto acknowledgement. This records application acknowledgement without a navigation that could trigger Cloudflare. It does not claim an independently queried server receipt. A pre-existing phrase or a still-enabled Vote button cannot confirm the vote.

If no such acknowledgement appears, the existing independent page-reload check remains a fallback. A post-click result that cannot be confirmed retains `vote_submitted` and its original outcome. It is not clicked again during that run. An unconfirmed submission still fails the workflow and therefore starts a fresh run under the unlimited recovery policy; the new run checks the page for cooldown before attempting another vote. Other bots can still retry within the current run. Unconfirmed outcomes remain failures, rather than being silently turned green; normal scheduled/manual runs are not deduplicated across runs by this in-memory flag.

Cloudflare checkbox interactions first wait for a strong image-template match, verify that its point hits a checkbox or a Cloudflare-owned iframe, require a stable target, and recheck after hover before sending native mouse input. The old nodriver `verify_cf()` accepted the best image match without a confidence threshold, even if no checkbox was present. A weak/missing match, disabled control, unrelated page element, or covered target now receives no click. Matching uses in-memory viewport screenshots and converts device pixels to CSS coordinates; it writes no temporary screenshot/template files. Logs separately record target type, match score, input sent, and trusted pointer events where the actual checkbox can be observed. For an opaque cross-origin iframe the event fields remain `null`; targeting the frame does not establish receipt inside it. Response/clearance checks and application authentication/vote confirmation still decide the result. Managed challenge pages are detected before their checkbox appears and may clear automatically without a click. These checks run wherever challenges appear: top.gg cookie authentication, Discord OAuth, before voting, after clicking `Vote`, and after vote verification reload. During top.gg authentication the runtime clears an active Turnstile before probing the Auth.js session endpoint, avoiding predictable protection-page 403 requests while the challenge is still active. For a denied session request, logs record only HTTP status and safe Cloudflare classification metadata (`cf-mitigated`, `cf-ray` if exposed), never response HTML or credentials. A 403 with server=cloudflare alone does not prove that a WAF rule blocked the request; an explicit `cf-mitigated: challenge` header is more specific. This improves diagnosis; it does not override a denial. top.gg privacy-consent overlays are checked repeatedly after page open/reload, then dismissed before auth probes, every marked click, vote interaction, and solver clicks so they cannot cover the checkbox or `Vote` button. If privacy-modal dismissal fails while the modal is detected, one screenshot plus the dismiss error is sent to Telegram. If solver cannot clear the challenge, the current browser page is captured when possible and sent to the configured Telegram chat immediately after the text report. Final `auth_failed` results also capture the last browser state and send it after the text report. No `SEND_ERROR_SCREENSHOTS` secret is required for CAPTCHA or final auth-failure evidence.

For other GitHub Actions diagnostics, add repository secret `SEND_ERROR_SCREENSHOTS=1`. Error and uncertain states then send screenshots to configured Telegram chat. Keep chat private: screenshots may contain Discord username, avatar, or top.gg account details. Screenshots are never uploaded as GitHub artifacts and local files are deleted after each Telegram delivery attempt.

> [!WARNING]
> Use ephemeral, single-tenant GitHub-hosted runners only. Do not run this project on persistent/shared self-hosted runners: browser processes handle live account credentials and temporary profiles.

Transient authentication/browser failures and protection-blocked authentication allow up to five total attempts per account (the first attempt plus four retries). In multi-bot runs, `error` or `uncertain` results retry only if no Vote submission may have occurred; `success`, `cooldown`, and `captcha_required` are final for the current run. Interactive CAPTCHA is intentionally not retried on the same runner/IP. Telegram reports identify accounts using a short SHA-256 fingerprint, never token fragments, and split automatically below Telegram's message limit.

Completed per-bot results survive later browser failures and authentication failures on a retry. Missing results become explicit errors. Duplicate bot IDs are collapsed while preserving order. All branches share one workflow concurrency group, and artifact validation/upload failures fail the workflow even when the voting process exits successfully.

- `success`, `cooldown`: final on the current runner/IP.
- A cooldown with a valid duration schedules an isolated dispatcher instead of sleeping/retrying on the same runner.
- `captcha_required`: final on the current runner/IP.
- `error`, `auth_failed`, `uncertain` before submission: up to five total attempts.
- `blocked` before submission: up to five browser attempts, then a fresh workflow run.
- Unconfirmed post-click outcomes: no resubmission within the current run; workflow failure starts a fresh run that checks for cooldown.

`error`, `auth_failed`, `uncertain`, or `captcha_required` sends its report first, then exits non-zero so GitHub Actions shows failure.

### Inspect the page without voting

In GitHub **Actions**, select **Inspect vote page (no submission)**, choose **Run workflow**, and run it on `master`. This manual diagnostic needs only the existing `BOT_IDS` and `TOPGG_COOKIES_JSON` secrets. It uses the first configured cookie session and the first bot, opens that vote page once, and records three observations before one bounded session check. It shares the voting workflow's concurrency group, so the two do not run simultaneously.

The diagnostic does not click Vote, start OAuth, solve a CAPTCHA, send Telegram messages, capture screenshots, or write the scheduler's `next-vote` artifact. It may dismiss the cookie-consent overlay. Logs contain fixed page/control categories, boolean signals, bounded counts and cooldown durations, and the existing credential-free HTTP denial metadata. They do not include cookie values, raw page text, account identifiers, or control URLs.

A green inspection means that observation completed; it does not mean that voting or authentication succeeded. A challenge page and a `403` session response confirm an access block during that inspection, but do not establish why another run failed or whether a cookie was revoked. The expired-cookie flag checks explicit expiry timestamps only; a false value does not validate the session. Because this workflow intentionally leaves CAPTCHA verification untouched, its result also cannot predict whether the regular voting workflow will clear a challenge.

## Project Structure

```text
auto-vote-topgg/
├── vote.py                          # Auth, vote, cooldown state, report, browser lifecycle
├── inspect_vote_page.py             # Manual page/session observation without submitting votes
├── test_inspect_vote_page.py        # Inspection privacy and no-submission checks
├── test_vote.py                     # Vote/auth/browser unit and regression tests
├── test_vote_regressions.py         # Partial results, bounded probes, browser-script regressions
├── test_scheduler.py                # Scheduler validation and dispatch regression tests
├── scheduler/
│   ├── Dockerfile                   # Northflank scheduler image
│   └── scheduler.py                 # Artifact-driven GitHub workflow dispatcher
├── audit_dependencies.py            # Stdlib OSV dependency audit
├── requirements.txt                 # Direct Python dependencies
├── requirements.lock                # Linux/Python 3.11 hashes and transitive pins
├── README.md                        # Setup and operating guide
├── SECURITY.md                      # Disclosure and credential policy
├── docs/
│   └── audit-2026-09-24.md           # Review findings, run evidence, and deployment limits
├── .github/
│   ├── CODEOWNERS                   # Sensitive-file ownership
│   ├── dependabot.yml               # Weekly pip/Actions updates
│   └── workflows/
│       ├── inspect-vote.yml         # Manual diagnostic; no vote or scheduler artifacts
│       ├── security.yml             # Tests, syntax checks, dependency audit
│       └── vote.yml                 # Secret handoff, vote, artifacts, cleanup
└── .gitignore
```

## Requirements

- Python 3.11+
- `nodriver==0.50.3`, `opencv-python-headless==5.0.0.93`, and `requests==2.34.2` as direct dependencies
- Hash-locked Linux x86_64 / CPython 3.11 dependencies in `requirements.lock`
- Google Chrome/Chromium from the pinned `ubuntu-24.04` GitHub-hosted runner image (discovered dynamically by workflow)
- Xvfb on headless Linux runners (the workflow uses the preinstalled copy when available and installs it only if missing)

`opencv-python-headless` is required by the Cloudflare target matcher, which uses nodriver's bundled checkbox template with a confidence threshold. The headless package supplies image matching without OpenCV GUI components.

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

Use pull requests and wait for `test`, `dependency-audit`, and `scheduler-image` to pass before merging. Repository settings are separate from this code: the September 24 review found `master` unprotected and no repository rulesets, so these checks were not enforced by branch protection at that time.

## ⚠️ Disclaimer

This project automates interactions using Discord user tokens. Using self-bots violates [Discord's Terms of Service](https://discord.com/terms). Use at your own risk. The author is not responsible for any account bans or other consequences.
