# Security Policy

## Supported Version

Security fixes apply to latest commit on `master`.

## Reporting a Vulnerability

Do not open public issue containing credentials, cookies, screenshots, or exploit details. Use [GitHub private vulnerability reporting](https://github.com/Snyri/auto-vote-topgg/security/advisories/new) for this repository.

Include affected commit, reproduction steps without live credentials, impact, and suggested mitigation if known.

## Credential Handling

This project handles Discord user tokens and top.gg Auth.js session cookies.

- Store credentials only in GitHub Actions Secrets or local secret files.
- Never commit `.env`, screenshots, browser profiles, or cookie exports.
- Use only ephemeral, single-tenant GitHub-hosted runners.
- Do not run on persistent/shared self-hosted runners.
- Rotate credentials immediately after suspected exposure.

Workflow secrets are handed to Python through mode-`0600` temporary files. Python unlinks those files, removes credential environment variables, then launches Chrome. Each browser uses temporary profile explicitly deleted after termination.

## Security Controls

- Immutable GitHub Action SHAs.
- Hash-locked Python dependencies.
- Weekly Dependabot updates.
- OSV dependency audit in CI.
- Secret scanning and push protection.
- Browser sandbox for non-root runners.
- Every Auth.js cookie forced to `Secure` during injection.
- Every Auth.js session-token cookie forced to `HttpOnly` during injection.
- Credential redaction in normal diagnostics.
- Separate write-capable cleanup and browser-startup retry jobs without user credentials.
- External Northflank scheduler uses a restricted GitHub token and consumes credential-free schedule artifacts.
- Cooldown artifacts contain only a bounded UTC epoch; no account IDs, bot IDs, cookies, or tokens.
- Scheduler validates bounded `next_vote_at` values, avoids duplicate active runs, and backs off after failed runs.
- CAPTCHA outcomes capture current browser view before profile cleanup and send it only to configured Telegram chat after the text report.
- CAPTCHA captions contain account fingerprint, bot ID, and escaped result detail—not tokens or cookies.
- Screenshot files are never GitHub artifacts and are deleted after every Telegram delivery attempt.
- nodriver Turnstile checkbox matching uses hash-locked `opencv-python-headless`; response/page clearance is observed without token injection.
- Fresh-run retry artifacts contain only a reason code (`browser_startup_failed` or `protection_blocked`); no credentials, account IDs, bot IDs, or screenshots.
- Fresh-run retry dispatch jobs hold `actions: write` only; no credential secrets, and automatic recovery is bounded to at most two fresh runs total and at most one retry per failure category.
- Chrome process stderr/stdout diagnostics are bounded to 600 chars and redacted through the same credential-scrub path as exception details, including parsed Auth.js cookie values.
- Browser startup, initial page open, shutdown, and late DevTools attachment are all time-bounded; a valid D-Bus session is preserved when available.
- Auth.js `__Host-` cookies are injected as host-only cookies using the top.gg URL, without a Domain attribute.
- The Northflank scheduler image pins Requests and all of its runtime dependencies explicitly, runs as non-root, and is built/tested in CI.
- OSV dependency checks retry bounded transient network/server failures instead of failing CI on a single temporary outage.

## Audit Log

### 2026-08-12

Added automatic fresh-runner retry on browser startup failure: credential-free marker artifact, single-dispatch guard, loop prevention via source label and run_attempt, bounded/redacted Chrome process diagnostics, and 5 regression tests.

Added hash-locked OpenCV headless support for nodriver's native Turnstile checkbox template matching, explicit click/clearance diagnostics, and bounded terminal fallback without challenge-token injection.

### 2026-08-11

Added mandatory CAPTCHA browser screenshots with report-first private Telegram delivery, escaped captions, path deduplication, and local deletion after attempted delivery.

### 2026-08-10

Added credential-free cooldown scheduling with timestamp-only one-day artifacts, bounded input validation, isolated `actions: write`, and single-dispatch lifecycle controls.

### 2026-08-07

Fixed browser credential inheritance, profile retention, weak cookie attributes, Requests CVE-2026-25645, transitive dependency integrity, and excess browser-job permissions.

Use pull requests and verified CI before merging. The September 24 repository API reported `master` as unprotected with no rulesets; source files alone do not enforce branch protection. Configure required checks and appropriate review rules in repository settings if enforcement is desired.


### 2026-09-20

Hardened authentication and scheduling after repeated protection-block failures: distinguish explicit unauthenticated sessions from temporary top.gg/protection blocks, use strong vote-page UI as an authentication signal, restore Discord localStorage injection on the Discord origin, cap same-run protection retries, emit bounded retry artifacts for failures, add scheduler validation/duplicate protection/tests, and scope workflow cleanup to vote runs.

### 2026-09-21

Pinned GitHub-hosted jobs to Ubuntu 24.04, added bounded late attachment and startup/shutdown timeouts for Chrome, preserved valid D-Bus sessions, corrected __Host- cookie injection semantics, expanded cookie-value redaction, added bounded protection-block fresh-run recovery, hardened scheduler dispatch ambiguity handling, pinned scheduler runtime dependencies, added scheduler-image CI, and moved the Northflank scheduler container to a non-root user.

### 2026-09-24

Bounded session fetches in the browser and Python, rejected malformed authentication probe results, and stopped probing during unresolved post-challenge transitions. Preserved completed per-bot results across later failures. Browser teardown warnings cannot discard results, and profiles are still removed after successful forced termination. Optional error screenshots are deleted even when Telegram is unconfigured.

Preserved scheduler source IDs across cycles, respected longer failed-run deferrals, and bounded artifact streaming. Recovery ZIPs now require exactly the expected filename and bounded contents without extraction. Workflow infrastructure failures propagate independently of voting status, and all branches share a concurrency group. OSV checks follow bounded pagination and fail closed on incomplete responses or unsupported lock entries.
