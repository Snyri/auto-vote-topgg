# Todo

- [x] Implement browser-startup fresh-run retry
  - [x] Add Python marker classification and artifact writer
  - [x] Add bounded Chrome startup diagnostics
  - [x] Add workflow marker validation, upload, and single dispatch job
  - [x] Add unit tests
  - [x] Update README, SECURITY, and lessons
  - [x] Run local verification
  - [x] Open PR and verify CI
  - [x] Merge PR and verify post-merge repository state

- [x] Solve CAPTCHA/Turnstile wherever it appears
  - [x] Root cause: post-vote Turnstile path reported CAPTCHA without invoking solver
  - [x] Use solver before terminal CAPTCHA reporting after Vote click
  - [x] Use solver before terminal CAPTCHA reporting after vote verification reload
  - [x] Add regression tests for post-vote solve success and fallback screenshot
  - [x] Run local tests

## Review

Browser-startup fresh-run retry merged in PR #18 and verified locally/CI/post-merge. Controlled forced-failure E2E remains unrun because implementation already merged and normal master path verified.

Post-vote CAPTCHA/Turnstile fix: root cause was direct `captcha_result()` after Vote-click/verification detection without `solve_turnstile()`. Fixed those paths to call solver first and only capture/report CAPTCHA after solver failure. Added 3 regression tests. Local suite: 73 tests OK.

- [x] Full reliability audit after protection-block retry storm
  - [x] Distinguish blocked session probes from explicit logout
  - [x] Restore Discord-origin localStorage token injection
  - [x] Add vote-page UI authentication fallback
  - [x] Bound same-run protection retries
  - [x] Emit retry schedule for failure states
  - [x] Harden scheduler timestamp validation, run identification, duplicate prevention, and timeout
  - [x] Add scheduler regression tests and CI syntax coverage
  - [x] Align scheduler dependency pin with repository requirements
  - [x] Scope cleanup to Top.gg vote workflow history

- [x] 2026-09-21 full post-success reliability pass
  - [x] Recover slowly-starting Chrome before process restart
  - [x] Redact individual Auth.js cookie values in diagnostics
  - [x] Avoid duplicate scheduler dispatch after ambiguous POST result
  - [x] Skip redundant Xvfb package install when runner already provides it
  - [x] Pin GitHub-hosted jobs to Ubuntu 24.04
  - [x] Run scheduler container as non-root and enable Docker Dependabot
  - [x] Bound security CI jobs with explicit timeouts
  - [x] Update reliability/security documentation
