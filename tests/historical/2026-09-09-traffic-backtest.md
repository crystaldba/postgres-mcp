# Historical outbound traffic-control backtest — 2026-09-09

**627 evaluable historical actions; zero unexpected fixed-code decisions.** Replayed all 718 available production outbound actions, July 16–September 9, 2026. Another 91 actions are inconclusive and excluded from pass counts.

Only `check_traffic` and its real `OutboundGatewayRepository` SQL were executed. This is not an end-to-end delivery or provider test. No production writes, sends, retries, wake repairs, or deployments occurred.

## Coverage

- 627 evaluable cases: 316 SMS, 252 email, 59 other operation controls; 295 distinct subject keys and 271 channel IDs.
- Calendar-month distribution: July 198, August 290, September 139.
- Snapshot: 718 actions, 4,187 state transitions, 219 messages inside relevant decision intervals.
- Two time bounds per action; counts above represent distinct historical actions, not repeated invocations.

| Original module decision | Fixed module decision | Evaluable actions |
| --- | --- | ---: |
| Pass | Pass | 551 |
| Stale context | Stale context | 59 |
| Stale context | Pass | 14 |
| Lease held | Lease held | 3 |

All 14 changes involve SMS activity addressed to different recipients. All 311 non-SMS controls retain their decisions. Within SMS: 261 unchanged passes, 41 preserved stale-context blocks, 14 corrected false blocks.

## Historical red/green evidence

Original code disagrees with recipient-scoped expectations in 14 evaluable cases; fixed code agrees in all 627. No SQL/probe errors were masked as fail-open passes. The fixed code also matches expectations for the current snapshots of the 91 inconclusive cases, but those are not claimed as historical passes.

For 23 evaluable actions whose production ledger recorded `stale_context`, the original-code replay reproduces all 23 blocks. Fixed code preserves 17 and removes 6 unrelated-recipient blocks. The remaining 8 of the 14 corrected cases are historical counterfactuals: they show how this module behaves on older production data, not proof those sends were blocked by traffic control at the time.

Jessica wake **26817**: original `stale_context`, fixed `pass`. Unrelated messages **694982** and **694983** no longer block her reply.

| Wake | Action timestamp (UTC) | Newer unrelated message IDs | Recorded stale-context failure |
| ---: | --- | --- | --- |
| 23954 | 2026-07-30 19:09:59.648028+00:00 | 374594, 374593 | No |
| 23963 | 2026-07-30 19:53:16.055241+00:00 | 375725, 375726, 375727 | No |
| 23969 | 2026-07-30 20:01:14.121714+00:00 | 375728, 375732, 375783 | No |
| 23971 | 2026-07-30 20:01:47.348256+00:00 | 376054 | No |
| 24059 | 2026-08-01 12:27:18.568097+00:00 | 429448 | No |
| 25378 | 2026-08-19 15:00:23.619112+00:00 | 661277 | No |
| 25434 | 2026-08-19 20:58:08.286149+00:00 | 661762 | No |
| 25471 | 2026-08-20 14:16:14.405242+00:00 | 662402, 662403, 662404, 662405, 662406, 662407, 662408 | No |
| 26184 | 2026-08-31 14:23:38.649482+00:00 | 683187 | Yes |
| 26189 | 2026-08-31 15:37:06.781214+00:00 | 683255 | Yes |
| 26193 | 2026-08-31 17:45:32.156863+00:00 | 683398 | Yes |
| 26205 | 2026-08-31 18:43:47.104320+00:00 | 683522, 683523 | Yes |
| 26467 | 2026-09-03 17:29:28.260514+00:00 | 687378, 687379 | Yes |
| 26817 | 2026-09-09 23:16:59.268736+00:00 | 694982, 694983 | Yes |

## Replay method and limits

Production was read in one repeatable-read, read-only transaction. Export omits message text, names, and unrelated raw payload fields. Phone values are replaced consistently while retaining punctuation, digit lengths, arrays, and nulls; subject keys are hashed. The saved snapshot is permission-restricted.

A disposable PostgreSQL 16 instance holds the snapshot. Time-filtered views expose messages and actions visible at each replay time; ledger states come from recorded transitions, and dispatch timestamps from the future are hidden. Production SQL and traffic decision code run unchanged against those views. A separate Python policy check validates recipient membership and lease/staleness outcomes.

The initial gate occurs after durable action creation and before its first state event. Both interval endpoints were replayed; cases whose evidence or decision changes within that interval are excluded. Saved wake timestamps later than the original action and messages updated after the replay time also cause exclusion.

Inconclusive reasons overlap: 72 actions have later-updated messages, 19 have later wake watermarks, and 2 have changing activity inside the decision interval; union 91. Do not interpret these as successful historical validations.

This uses retained database history, not archived WAL or complete point-in-time backups. Deleted rows and unrecorded historical changes to identity/raw-event metadata cannot be reconstructed. Original traffic-mode/override settings are not replayed: the module is evaluated with `override=False` at initial action creation. Later retries, provider dispatch, and unrelated modules are outside this backtest.

## Reproduce

From `/home/danpark/projects/postgres-mcp-comm`:

```sh
.venv/bin/python tests/historical/backtest_gateway_traffic.py \
  --snapshot .cache/gateway-historical-20260909.json \
  --report .cache/gateway-historical-report-20260909.json
```

Replay needs Docker and the local snapshot; no production connection required. Runner exits nonzero for fewer than 100 evaluable actions or any candidate-policy mismatch. Capture is separate, explicitly enabled with `--capture` and `GATEWAY_BACKTEST_PRODUCTION_DSN`.

Baseline commit: `12ebe21355d393edd287fa97aa7c6bfbb9eba2d1`.

Candidate repository SHA-256: `579167745625089ab1758457f8c7bc88436db3d4958566b7b94eaafe9581df05`.

Snapshot SHA-256: `359ff7f7af5d86727e216f7201869004120cfca7ef03a508864766c00b3fd5b2`.

Artifacts: [replay runner](backtest_gateway_traffic.py), [local sanitized snapshot](../../.cache/gateway-historical-20260909.json), [case-by-case results](../../.cache/gateway-historical-report-20260909.json). Snapshot/results are local ignored artifacts; retain them to reproduce this exact run.
