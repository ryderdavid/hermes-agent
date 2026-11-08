# Hermes Agent — ryderdavid fork

This repository (`ryderdavid/hermes-agent`) is a fork of
[`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent).
It carries personal gateway customizations (currently: semantic tool-activity
labels on gateway events for the Android app) while staying rebased on
upstream `main`.

## Why a fork

The Android app (`ryderdavid/hermes-android`) renders gateway `tool.*` events
as activity cards. Upstream ships a redacted 80-char `context`/`label` preview
and full `args`; the fork composes a proper semantic label (curated verb +
preview, localized via `display.language`) into the event payloads so clients
can show "Editing …/card.dart" / "Running the test suite" without re-deriving
it. Core-surface changes follow upstream's footprint ladder (AGENTS.md): the
payload change is presentation-only, in `tui_gateway/`.

## Staying current with upstream

`scripts/sync-upstream.sh` mirrors the pattern from `ryderdavid/hermes-android`:

1. `git fetch upstream`, fast-forward local `main` to `upstream/main`,
2. rebase the work branch onto it,
3. run a smoke test subset (`python -m pytest tests/ -q -k "tool_progress or session_history or display"`),
4. on rebase conflict: stop and file a bead (`bd create`) describing the
   conflict so the integration is tracked, never force-pushed over.

Nothing is pushed unless `--push` is given.

Run it via the slash command in `commands/sync-upstream.md` (symlink or copy
that file into `~/.hermes/commands/` once), or directly:

```bash
scripts/sync-upstream.sh                 # sync current work branch
scripts/sync-upstream.sh --branch feat/x --push
```

## Update contract with the Android app

The gateway already advertises `version` in `session.info`
(`tui_gateway/server.py::_session_info`). The fork additionally stamps
`"fork": "ryderdavid/hermes-agent"` and `fork_ref` there when built from this
fork, so `ryderdavid/hermes-android` can show a "gateway update available"
banner when the connected gateway is older than the fork's required minimum
(app-side bead in the hermes-android repo).

## Running the fork locally

The live gateway is installed from upstream
(`uv tool install git+https://github.com/NousResearch/hermes-agent`). To run
this fork instead, reinstall from the fork and restart the gateway **between
sessions** (a running session rides on the current gateway process):

```bash
uv tool install "git+https://github.com/ryderdavid/hermes-agent@main"
# then restart the gateway service / `hermes gateway` process
```

Never switch the installed gateway mid-session.
