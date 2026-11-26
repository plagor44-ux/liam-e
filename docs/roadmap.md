# Roadmap

This page shows where liam-e is heading. It is a direction, not a schedule or a promise: plans
change, and the [open issues](https://github.com/plagor44-ux/liam-e/issues) remain the source of
truth. Questions and ideas are welcome in
[Discussions](https://github.com/plagor44-ux/liam-e/discussions).

## Now

Work that is in progress or close to done.

- **Skills in settings** (#156): list skills, pause them, and remove the ones drafted in chat.
- **Chat follow-ups** (#163): citation errors, banner focus and narrow layouts.
- **Rule editor fixes** (#161): a clipped notice and the draft state of the active toggle.
- **Gmail connector rate limits** (#164): handle provider rate limits during mail actions.
- **Installable app** (#98): harden PWA installation, offline navigation and app updates.
- **Voice release evidence** (#26): evidence that speech-to-text and text-to-speech work in a
  Studio deployment.

## Next

- **Sign-in on Studio's MCP route** (#157): check the sign-in live; an offline probe exists.
- **MCP endpoint** (#36): serve the read-only mail tools as a Streamable HTTP MCP endpoint
  behind the Studio sign-in, with a tested Vibe setup. It starts once #157 succeeds.

## Later

- **Remote MCP clients** (#54): connect other assistants through OIDC and OAuth. This needs
  proven access control first; until then the [agent read facade](agents/agent-read-facade.md)
  rejects every request.

## Good places to start

Small, well-defined issues for a first contribution:

- Unit tests for account status and connection type (#166).
- Unit tests for the IMAP presets and the mail host allowlist (#167).
- IMAP/SMTP presets for more mail providers (#168).

All issues labelled
[`good first issue`](https://github.com/plagor44-ux/liam-e/labels/good%20first%20issue) or
[`help wanted`](https://github.com/plagor44-ux/liam-e/labels/help%20wanted) are open for
contributions. [CONTRIBUTING.md](../CONTRIBUTING.md) explains the setup and the checks.
