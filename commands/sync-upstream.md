---
description: Safely integrate the upstream NousResearch/hermes-agent update into the ryderdavid fork
---

# /sync-upstream

Safely pull upstream `NousResearch/hermes-agent` changes into the
`ryderdavid/hermes-agent` fork without breaking fork customizations.

Run `scripts/sync-upstream.sh` from the fork clone (`~/Code/hermes-agent`).
It fast-forwards `main` to `upstream/main`, rebases the current work branch,
runs the smoke subset (`pytest -k "tool_progress or session_history or
display"`), and — on a rebase conflict — stops and files a bead in this repo
describing the conflict instead of force-pushing over it. Add `--push` to
push `main` and the work branch (force-with-lease) after it passes.

Fork customizations the sync must preserve (see docs/FORK.md): semantic tool
labels in `tui_gateway` event payloads, the `fork`/`fork_ref` marker in
`session.info`. If upstream changed those areas, resolve in favor of keeping
the fork contract and update `ryderdavid/hermes-android` if the payload shape
changed (bead lives in that repo).
