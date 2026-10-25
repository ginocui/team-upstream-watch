# Local state and operations

## Lifecycle

1. Keep one team config and one writable `state_root` for a collector.
2. Run `validate` without network or state creation.
3. Run `tick --allow-public-network` from your existing scheduler. It checks only sources that are due, up to `max_checks` and the work-start budget.
4. Members read `inbox --member ID`, examine the underlying public source and make their own decisions.
5. `ack` records a member/event receipt. It neither installs anything nor clears another member's work.

There is no daemon, web listener, chat delivery or agent wakeup in this release. A scheduler failure does not mean a source was checked. Source `checked_at` and `last_good_checked_at` are Unix timestamps for actual source observation; they are not the time when the inbox command was run.

## State format

The collector's `tracking.sqlite3` stores `tracking_state`, `runs` and `review_outbox`. The team entry adds `team_receipts(member,event_id,receipt)` with a composite primary key. The table is additive; it does not reuse the collector's global receipt field. The first package uses config schema `team-upstream-watch/1`.

A repeated identical member/event receipt is idempotent. A different receipt is a conflict, not a silent update. Receipts are statements made by the caller; member identity and the truth of the review are not authenticated.

Source definitions are bound to each recorded event. If you reuse an ID for a different repository or observation scope, the old events remain in canonical history but are not shown as events from the new source. Removing a member or a source from config does not erase records or revoke filesystem access.

## Failure and recovery

- `UNKNOWN` retains the prior successful snapshot; `PARTIAL` does not mean complete coverage.
- Repeated equal observations do not continually create the same event. Recovery can create a recovery event without implying a new release.
- SQLite transactions serialize collection. If the collector is busy, do not start another competing writer to force progress.
- The database commit can succeed before the human-readable run file is written. A later projection error does not undo the database. `inbox` can read those committed events even if the additive team receipt table has not yet been created; it opens SQLite read-only and treats that absent table as zero receipts. A valid member ACK can initialize the receipt table within its serialized write transaction. An ACK before any collection is refused without creating a database or state directory. Fix the projection blocker before retrying collection; do not delete committed history.
- A source timeout or a Git/API/feed restriction does not justify injecting invented versions or automatically advancing an adoption baseline.

For a simple local backup: stop your scheduler and other writers, copy the complete state directory to a separate owned backup location, then verify you can restore and read that copy before relying on it. This package does not provide an online/offsite backup service or a tested power-loss recovery guarantee. Never copy an active SQLite database as if that were a consistent backup.

Restoring old code is not restoring old data. Restoring old data can also make old events visible again. Do not delete receipts, change event IDs or create a second state directory merely to hide an unknown external action in a downstream integration.

## Supported boundaries

Python 3.11+ is the declared syntax/library floor. Tests were run on the execution environment reported in the release notes; a CI matrix is not a claim that all its jobs have passed. The public team config uses interval-based due checks, not the original collector's fixed-zone weekly schedules.

The original lower-level collector CLI has extra discovery and local-view mechanisms. The supported team guide uses only explicit public sources, member inboxes and team receipts. WP17 and production Runner-specific routes are not available in the public package.

GitHub/Hugging Face service quotas, TLS and network availability remain external dependencies. Git is an executable in your environment, not a bundled sandbox. Follow your platform's normal credential/configuration controls; no absolute anonymity guarantee is made for a hostile host.
