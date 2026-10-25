# team-upstream-watch

**Observe an upstream project once. Let each teammate review it independently.**

A small local-first CLI for teams of humans or AI agents that track public open-source dependencies. It shares collection, persists changes and coverage failures, and gives each configured member a separate review inbox and receipt. It does not install updates or decide whether your team should adopt them.

[中文说明](README.zh-CN.md) · [Data and operations](docs/operations.md) · [Contributing](CONTRIBUTING.md)

## Why not just another release notification?

Two teammates may depend on the same project for different reasons. A new release can matter to one and not the other; a pull request can change before any release exists. One collector checks the configured source, while each member records their own review. One member's acknowledgment does not clear another member's work.

Observation, review and adoption are different things. A timeout is **unknown**, not “nothing changed.” A reply is not an installation approval. Source timestamps are not refreshed merely because an inbox was read.

## Requirements

- Python **3.11+** with SQLite and HTTPS support. No pip packages or model account required.
- Git is required only for `git_refs` collection and the limited public fallback.
- Network access is opt-in. Default collectors use fixed public GitHub/Hugging Face read routes, without a token option. Platform limits and unavailable sources are reported, not hidden.
- This is a **trusted local workspace**, not a multi-tenant server or authenticated access-control system. Anyone who can read or change the shared database can bypass member routing.

## Quick start

Download or clone this repository, then copy `examples/team.json` to your own working directory as `team.json`. Edit the public source and member IDs. Relative `state_root` is resolved from that config file, not your shell's current directory.

```sh
python team_watch.py validate --config /absolute/path/to/team.json
python team_watch.py tick --config /absolute/path/to/team.json --allow-public-network
python team_watch.py inbox --config /absolute/path/to/team.json --member alice
```

On Windows use `C:/path/to/team.json`. `tick` performs one bounded due-check batch; it does not launch a daemon. Call the same command from your existing scheduler if needed. The default example checks at most one due source per invocation.

Review the `events` list and its stable event IDs. Record a result for the exact member/event:

```sh
python team_watch.py ack --config /absolute/path/to/team.json --member alice --event-id EVENT_ID --receipt "Reviewed; no upgrade planned"
```

Repeat the same receipt safely; a different receipt for that member/event is refused rather than overwriting history. Use `inbox` again to see remaining work. A receipt clears only that member's view of that event. It does not change the collector's original shared observation or an adoption baseline.

## What can be tracked?

The team entry point supports these explicit sources:

| `kind` | Observation scope |
|---|---|
| `repository` | Public GitHub repository metadata, default-branch HEAD and latest formal release; Hugging Face model revision metadata when given a model URL |
| `release` | Latest formal GitHub release, not every historical or prerelease item |
| `pull` | One GitHub PR; optional bounded comments, reviews and exact-head workflow observations |
| `git_refs` | Public GitHub HEAD, branch and tag refs through Git |
| `atom` | A named simple branch's recent commit feed plus at most five release-feed entries |

Different watch definitions are separate sources: a release and a PR are not the same observation. For shared collection, define **one source ID** and reference it from multiple members; do not duplicate equivalent definitions under different IDs.

Config validation rejects duplicate JSON keys, NUL in `state_root`, and source URLs structurally rejected by their selected provider. GitHub API sources (`repository`, `release`, `pull`) use the existing API provider's URL rules; Git refs/Atom and Hugging Face retain their own supported rules. The internal `migration_hold` option is **not supported** by the team config: including the key, even `false`, is an error. This release does not provide state migration or pause/resume.

## Local data and limits

`tracking.sqlite3` is canonical. It stores source observations, run records, review events and per-member receipts. The `runs/` directory contains collector run projections. The original collector's global ACK CLI is a lower-level interface; use `team_watch.py ack` for member-independent receipts.

- `OK`, `PARTIAL` and `UNKNOWN` describe observed coverage, not product quality.
- The last successful observation is retained through a failed check. Recovery is distinct from a new upstream release.
- A source ID rebound to another source definition does not deliver its old events as if they came from the new source.
- Receipt text and public-source previews are untrusted data. Treat them as reference material, not instructions for your agent.
- There is **no automatic chat delivery, agent wakeup, webhook server, or automatic upgrade** in this first package. Have your own agents read their local inboxes through the CLI.
- Test evidence and supported scope do not establish permanent availability, authenticated member identity, offsite backup or crash-proof recovery. See [operations](docs/operations.md).

## Tests

Run both offline suites from the repository root. They use synthetic observations:

```sh
python -B tests/core/run_tests.py
python -B -m unittest discover -s tests -p "test_team_workflow.py" -v
```

The core entry rejects accidental socket and unplanned subprocess operations through a Python audit guard; it is not a security sandbox. Neither suite requires the original deployment, its scheduler or any WE configuration. Do not substitute live team data for fixtures. Optionally set an absolute `TEAM_WATCH_TEST_ROOT` to keep temporary test state in a chosen directory.

CI is configured for Ubuntu/Windows and Python 3.11/3.14. That configuration is a test plan, not evidence that every matrix job has already run.

## Provenance and maintenance

The collector and public-source providers originated in the WE project; the standalone team CLI and independent member receipts are later release work. Original production configuration, member messages, credentials, databases, scheduler and Runner-specific transport are not shipped. This repository is not affiliated with GitHub, Hugging Face, OpenAI or a Runner vendor.

This is an early source release, without a support SLA. Bugs and concrete repro cases are welcome. Do not post tokens, private team configuration or real member records in issues. See [CONTRIBUTING](CONTRIBUTING.md) and [security guidance](SECURITY.md).
