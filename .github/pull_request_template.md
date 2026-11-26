<!--
Fill in every section; write "none" where nothing applies. State verified facts, not intentions.
Never include credentials, tokens, real mail or appointments, Mistral IDs, local paths or raw
provider responses.
-->

<!-- One closing keyword per issue, e.g. "Closes #12. Closes #13." -->
Closes #

## Summary

<!-- What changed and why, in terms of observable behavior. For multiple issues add a section "### #12: <title>" per issue. -->

## Verification

<!-- Commands actually run with results (exit code, counts). Identify what was not run. Tests with fakes do not prove behavior in Studio, with providers or on devices. -->

- `bun run check`:
- `bunx nx run web:test`:
- `bash tools/uv.sh run --no-sync pytest tests apps/api/tests packages/py -q`:
- `RUN_PG_CONTRACT=1 bunx nx run db:test-pg-contract`:
- `git diff --check`:
- Targeted or live checks:

## Contracts

- [ ] One task, one branch, this one PR; each issue has its own commits and closing keyword above
- [ ] API changed: `apps/api/openapi.json` and `apps/web/src/api/generated/` regenerated and committed (`bash tools/gen-types.sh --check` passes)
- [ ] Tables changed: migration generated with `bunx nx run db:revision`, not written by hand, and applied with `bunx nx run db:migrate`
- [ ] New settings as a module under `packages/py/env/src/env/`, each name empty in `.env.example`, secrets as `WorkspaceSecret`
- [ ] New web route: `bunx nx run web:gen-routes` run
- [ ] New mutating operation in `MUTATING_OPERATIONS` (`apps/api/tests/test_mcp_exposure.py`)
- [ ] The agent still changes or sends only after confirmation of a proposal card
- [ ] UI text in both the German and English catalogues, following DESIGN.md; code, comments and commits in English

## Risk and rollback

<!-- What could break, for whom, and how to roll it back (migration, data). -->

## Review and agent participation

<!-- Author (human or agent with model). Independent reviews: reviewer, model family, outcome, findings fixed and findings rejected with reasons. -->

- Created by:
- Independent review:

- [ ] This PR contains no credentials, real mail or calendar content, Mistral IDs, local paths or raw provider responses.
