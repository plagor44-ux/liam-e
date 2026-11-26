# Contributing

Thank you for your interest in liam-e. Bug reports, documentation fixes, tests and features are
all welcome. liam-e has a single maintainer, so reviews may take a while; for anything larger
than a small fix, please open an issue first so we can agree on the approach before you spend
time on it.

Everyone who takes part follows the [Code of Conduct](CODE_OF_CONDUCT.md). Suspected
vulnerabilities do not belong in an issue or pull request; report them privately as described
in [SECURITY.md](SECURITY.md).

## Issues

Issues use the forms in [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/). Most contributions
start with a [bug report](.github/ISSUE_TEMPLATE/01-bug.yml) or a
[change request](.github/ISSUE_TEMPLATE/02-change.yml); the
[evaluation](.github/ISSUE_TEMPLATE/03-evaluation.yml) and
[subtask](.github/ISSUE_TEMPLATE/04-subtask.yml) forms split up larger work. Each form asks for
acceptance criteria and how to verify them, so that a person or a coding agent can implement the
issue and a reviewer can check it.

Every issue follows exactly one form: its sections in the form's order and its label. In the
browser GitHub offers only the forms. The API and `gh issue create` accept any text and cannot fill
in a form, so an issue created from the command line, by a person or a coding agent, goes through
[`tools/issue_form.py`](tools/issue_form.py):

```bash
body="$(mktemp)"   # outside the checkout, so the draft is never committed
bash tools/uv.sh run --no-sync python tools/issue_form.py skeleton 02-change > "$body"
# Answer every section in "$body" and delete the hint comments, then:
bash tools/uv.sh run --no-sync python tools/issue_form.py create 02-change --title "..." --body-file "$body"
```

`create` checks that the body has exactly the form's `###` sections in order, with every required
field answered, and adds the form's label. Like the browser, it starts the title with the form's
title prefix, `Eval: ` for evaluations. `verify <number>` checks an existing issue, for example
after an edit.

## Set up and check your change

The [local development guide](docs/local-development.md) covers installation, the database,
startup and the synthetic sample data. Neither the tests nor the sample data need mail accounts,
provider access or Mistral credentials.

Before you open a pull request, run the checks that CI runs:

```bash
bun run check                # build, UI type check, generated-client drift check
bunx nx run web:test
bash tools/uv.sh run --no-sync pytest tests apps/api/tests packages/py -q
RUN_PG_CONTRACT=1 bunx nx run db:test-pg-contract   # needs Postgres with pgvector
git diff --check
```

If you cannot run one of them, for example the Postgres contract suite, say so in the pull
request.

## Rules

[AGENTS.md](AGENTS.md) holds the binding rules for architecture, conventions and security, the
domain notes under [docs/agents/](docs/agents/) those for each area, and
[DESIGN.md](DESIGN.md) those for the UI and its text. Coding agents read the same files, and the
same rules apply to contributions written with their help; the person who opens the pull request
is responsible for it. In brief:

- **English.** Code, comments, docstrings, documentation, issues, pull requests and commit
  messages are in English. UI text lives in the typed German and English catalogues; add every
  new key to both.
- **Synthetic test data only.** Use addresses under `example.org`, `example.com`, `example.net`,
  `*.example` or `*.test`, and invented names and content. Never use a real mail, appointment or
  header as a fixture, even shortened. Screenshots show only the sample data from
  `api.mail.dev_seed`.
- **No secrets or private data.** Passwords, app passwords, tokens, API keys, OAuth codes,
  cookies, Mistral organization, workspace or app IDs, real mail or calendar content and
  absolute local paths never go into files, issues, pull requests, commit messages or logs. CI
  runs gitleaks over the whole history and `tests/test_public_repo_hygiene.py` over every tracked
  file. A secret that was committed counts as burned: it must be rotated, not just removed.
- **Confirmation first.** The assistant never sends or changes anything without a confirmed
  proposal card. A new mutating API operation goes into `MUTATING_OPERATIONS` in
  `apps/api/tests/test_mcp_exposure.py`; the regenerated `apps/api/openapi.json` lists it.
- **Generated files stay generated.** After an API change, regenerate and commit the OpenAPI
  specification and the web client (`bunx nx run api:gen-openapi`, then
  `bash tools/gen-types.sh`). A table change needs a migration generated with
  `bunx nx run db:revision`, never a hand-written one. A new web route needs
  `bunx nx run web:gen-routes`.
- **One task, one pull request.** A task produces exactly one branch and one pull request, even
  when it covers several issues. Each issue gets its own commits and its own closing keyword
  (`Closes #12. Closes #13.`). Commit messages follow the Conventional Commits style used in the
  history, for example `fix(calendar): ...` or `docs: ...`.

## Pull requests

Pull requests follow [`.github/pull_request_template.md`](.github/pull_request_template.md).

- Describe what changed in terms of behavior, and list the checks you ran with their results.
  Tests with fakes do not prove behavior in Studio, at a mail provider or on a device; state what
  you did not check.
- CI must pass. For pull requests from forks, the maintainer may need to approve the CI run
  first.
- Keep the diff small and follow the patterns of the surrounding code; module docstrings
  describe local conventions.

## License

liam-e is licensed under the [Apache License, Version 2.0](LICENSE). Unless you state otherwise,
a contribution you submit is licensed under the same terms, as section 5 of the license
describes.
