# Lessons

- Before marking a repository task complete, verify open PR count, pending/failed required checks, and open security alerts. Background completion is not task completion.
- GitHub may redact a numeric timestamp job output when secret scanning classifies it as sensitive. For cross-job control state, use an artifact and verify the downstream job receives the value during a live run; artifact upload alone is insufficient proof.
- GitHub workflow disable is not idempotent and disabled workflows may not resolve by display name in `gh run list`. Read current state before disabling and use stable workflow filenames/IDs for lifecycle cleanup.
- A cleanup claim must state its scope. Before calling workflow history clean, count runs repository-wide and verify every workflow covered by the stated retention policy—not only the workflow that owns the cleanup job.
- Before relying on a dependency's convenience method, inspect its installed implementation and production logs for undeclared optional runtime requirements. nodriver 0.50.3 `verify_cf()` calls OpenCV-based `template_location()`; without hash-locked `opencv-python-headless`, no checkbox coordinates or click are produced.
- A nodriver `Failed to connect to browser` can mean Chrome is alive but DevTools is late, or that the runner really needs replacement. First attempt a bounded late attach to the existing process; if startup still fails, signal a credential-free marker artifact so a fresh GitHub runner can recover without exposing credentials.
- CAPTCHA/Turnstile policy must be phase-independent: if a challenge appears after clicking Vote or after verification reload, call nodriver `verify_cf()` via the shared solver before reporting captcha_required. Terminal CAPTCHA reporting is only after solver failure, not on first detection.
- Before clicking Turnstile or vote controls, clear site-level consent/privacy overlays. Visual blockers can cover the target while DOM detection still says the target exists, causing verify/click flakiness.
- For random modal blockers, guard the lowest click primitive as well as page-level flow. Call overlay dismissal immediately before the actual click, because a modal can appear between earlier checks and the click.
- When an automatic blocker-dismiss step fails while a blocker is present, report the failure at the user boundary with a screenshot and redacted reason, then dedupe to avoid Telegram spam.
- Quantcast consent modals can place visible text inside child spans, so privacy-dismiss detection must prefer stable selectors like #accept-btn and include textContent/id fallback, not only innerText.
- Privacy/consent modals may appear after navigation completes and after an initial sleep; use a short settle loop after page open/reload before checking auth or clicking Login.
- Final top.gg auth_failed after all retries needs browser-state evidence, not only text report. Capture on the last retry only and send after the report to avoid retry spam.

- A 403/HTML response from an Auth.js session endpoint is not equivalent to an expired cookie. Preserve HTTP/content-type diagnostics and distinguish explicit unauthenticated JSON from an upstream/protection block.
- Do not write another origin's localStorage through a cross-origin iframe. Browser same-origin policy blocks it; navigate to the target origin before setting its localStorage.
- When the vote page itself exposes a strong authenticated voting surface, do not let a separately blocked session-probe endpoint create a false logout.
- Every failed automation cycle needs a bounded next-attempt timestamp; otherwise an external scheduler can create a retry storm when failures produce no artifact.
- Retry policy must match failure scope: a protection block tied to runner/IP should get at most one fresh-browser retry, then defer instead of repeatedly hammering the same runner.
- Workflow cleanup should be scoped to the workflow it owns; repository-wide retention can silently erase diagnostic history from unrelated CI.
- External scheduler dispatch must snapshot existing workflow-run IDs before POST and identify a genuinely new run afterward; a timestamp-only heuristic can attach to another concurrent dispatch.

- nodriver 0.50.3 gives Chrome only a short internal DevTools discovery window; if its subprocess is still alive after "Failed to connect to browser", poll the existing DevTools endpoint for a bounded late-attach window before killing Chrome. Restarting a healthy-but-slow process wastes a profile and can amplify runner flakiness.
- A workflow-dispatch POST can succeed server-side while the client sees a timeout. Snapshot existing run IDs before POST and, on an ambiguous transport failure, search for the new run before sending another POST.
- Redaction should include parsed credential components, not only the original serialized secret blob; an exception may expose one cookie value without reproducing the complete JSON secret.

- Browser/profile cleanup must never override a completed vote result: cleanup failures are operational warnings, while retrying a vote because teardown failed can create duplicate attempts.
- Fresh-run recovery can safely cross failure categories once (for example protection block → startup failure), but must track depth and prevent repeating the same recovery category indefinitely.
- Scheduler artifacts are control-plane inputs: validate archive size, exact filename, exact JSON shape, and timestamp bounds before acting on them.

- Do not probe Auth.js through an active Turnstile/protection page. Clear the visible challenge first, re-check strong page state, and only call the session endpoint when the page remains ambiguous; this removes predictable 403s without weakening authentication checks.

- Never accept a vote as successful from the same DOM that received the click. Run #132 produced a false positive while the vote remained available. Require an independent reload, strong success/cooldown evidence, and no enabled Vote button before scheduling the next 12-hour success window.
- A persistent scheduler must refresh its target while sleeping. A later manual run can supersede an earlier artifact; caching the old epoch until wake-up leaves the external scheduler offset even when GitHub has a newer correct schedule.

- Treat vote confirmation as a high-integrity state transition: require repeated independent reload confirmations on the exact bot vote URL, reject enabled Vote controls/login redirects, and prefer false negatives (`uncertain`) over false positives (`success`).
- An observed cooldown without a parseable remaining duration must not manufacture a new 12-hour schedule from observation time; recheck soon instead.

- More verification is not automatically safer. After the #132 regression, require one independent persisted reload with exact-page/no-login/no-enabled-Vote checks; use a second reload only when the first result is ambiguous. This preserves false-positive protection without unnecessarily multiplying Cloudflare challenges.
- Cooldown parsing must sum compound durations (for example `10 hours 59 minutes`); using only the first component can schedule the next attempt almost an hour early.

- Runs #136 and #138 showed that verification reloads can trigger recurring protection challenges even after a real vote. After an independent reload encounters a challenge, briefly observe the same reloaded page without another reload; if still ambiguous, retain `uncertain` and let the existing retry check for cooldown. Never turn an unverified click into `success`.
