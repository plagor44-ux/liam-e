# PWA acceptance on iPhone (#98, 2026-11-25)

Status: **Passed.** On 25 November 2026 the owner accepted the app installed from the Studio test
app on an iPhone, with the overall result “PWA ist ok” (“the PWA is OK”). This record keeps what
the owner observed separately from what only the overall pass covers.

## Scope

#98's verification has two parts:

1. Reproducible checks of the production build with synthetic data: manifest, icons and worker, a
   new installation, start and return, deep links, small viewports and the update. These run in
   headless Chromium in `tests/pwa_matrix.cjs` and in the CI job `pwa-matrix`, described under
   “Local PWA check” in [local development](../../local-development.md).
2. Acceptance of an installed iPhone app: start from the home screen, keyboard and safe areas, the
   Studio sign-in and an update in a reachable build. This record covers that part. #79 accepts the
   Safari swipe gesture separately.

## Automated evidence

The CI job “PWA matrix (Chromium)” passed on each commit deployed during the session:

| Commit on `main` | Change | PWA matrix |
| --- | --- | --- |
| `8c3962f8` | Studio acceptance tooling (#158) | success |
| `8109b525` | Offline Studio MCP-route sign-in probe (#159) | success |
| `4abf1f08` | Calmer chat start page (#162) | success |

On Chromium the matrix covers installability, worker registration and immediate claim, the
standalone start URL, deep links, four viewports, offline navigation in English and German with an
empty CacheStorage, and a v1 → v2 update with two open tabs and stale-chunk recovery. It does not
cover Safari or iOS.

## Manual acceptance on iPhone

| Field | Value |
| --- | --- |
| Date | 25 November 2026 |
| Device | iPhone (model not recorded) |
| iOS | 19.1 |
| Safari | 19.1 |
| Install target | Studio test app |
| Deployed during the session | `8c3962f8` → `8109b525` → `4abf1f08`, all on `main` |

The owner worked through the steps below and reported an overall pass. Individual results exist
only for A2 and the draft part of A7.

| Step | What the step checks | Result | Evidence |
| --- | --- | --- | --- |
| A1 | Add to Home Screen from Safari after the Studio sign-in; icon and title behind the gateway | Covered by the overall pass | Not observed individually |
| A2 | Start from the home screen; a Studio sign-in happens inside the standalone app | **Passed** | Observed: the sign-in stays inside the standalone app window, with no sign-in loop |
| A3 | Chat composer with the keyboard open stays visible without a layout jump | Covered by the overall pass | Not observed individually |
| A4 | Safe areas and status bar in light and dark appearance | Covered by the overall pass | Not observed individually |
| A5 | Offline cold start from the home screen shows the offline page in the app language | Covered by the overall pass | Not observed individually |
| A6 | Back online after at least 60 s in the background, the update check runs; with no new version nothing visible happens | Covered by the overall pass | Not observed individually |
| A7 | An unsent composer draft survives the deploy of a different later commit without a forced reload | **Passed** | Observed: the draft remained after the update |
| A7 | After that update, Tasks loads, or reloads once, without an error page | Covered by the overall pass | Not reported individually |

Both deploy steps of the session changed files under `apps/web`. `8c3962f8` → `8109b525` includes
14 web files from #160, and `8109b525` → `4abf1f08` changes 20. Each step therefore shipped a
different web build, not a redeploy of the same commit, which would keep the asset hashes. The
owner did not say which step the draft check spanned. This paragraph is derived from Git, not
observed on the device.

## Not recorded or not covered

- Individual results for A1, A3, A4, A5, A6 and the Tasks part of A7. They are covered only by the
  overall pass, with no per-step notes or screenshots.
- The iPhone model and which deploy step the draft check spanned.
- On iOS: switching over with two open tabs, stale-chunk recovery and an empty CacheStorage. Only
  the Chromium matrix checks these.
- Android, iPadOS, other browsers and desktop installs.
