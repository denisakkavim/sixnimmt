# Traces and replay

Set `RunConfig(trace_dir=Path(...))` or CLI `--trace-dir` to preserve an experiment.
The directory must not already exist. Without tracing, `run_arena` retains
aggregate results rather than every match's full history; `run_match` returns
its individual result with events.

## Files

Use each manifest match entry's filenames rather than guessing generated names.

| File | Contents |
| --- | --- |
| `manifest.json` | Versioned run configuration, seats, outcomes, filenames, seeds, and statistics |
| `<match>.jsonl` | Complete authoritative event history, including private/admin events |
| `<match>.actions.jsonl` | Attempt records and decision measurements |
| `<match>.model.jsonl` | Optional raw model requests, responses, and parsing diagnostics |

Each JSONL line is one JSON object. Event and action writers can add
`player_display_names` labels to aid inspection; the readers reconstruct the
typed event/action models independently of those labels.

Manifest version 1 includes `rules`, `protocol`, resolved `run_config`, `seats`,
the root `seed`, requested/started/completed counts, and `reproducible`.
Per-match entries include `game_index`, `match_id`, derived `seed`, `outcome`,
`winners`, `ended_by`, `reason`, `log`, `actions`, `seat_stats`, and `stats_errors`.
The manifest is published through a temporary file and rename; it is not a live
per-action progress feed. An abruptly terminated process may leave logs without
a completed manifest.

## Ordering and privacy

Events have a type, match ID, audience, payload, timestamp, and sequencing
metadata. `seq` orders events; `server_action_seq` associates events with their
action order. The latter field retains its historical name in the current
trace schema. Timestamps are diagnostic, not the basis for replay ordering.

Action records contain the action ID, actor, type, observation reference,
sequence, receipt time, outcome (`accepted`, `rejected`, `timeout`, or `error`),
reason, and optional decision start/end/duration measurements. Atomic proposals
can produce multiple records for one bot call; latency is counted once for that
decision rather than once per action.

Player view versions count only events visible to that player. Global sequence
gaps must not become observations about hidden activity. Full trace files are
privileged data; use audience filtering and view folding to construct player
observations, not the raw JSONL stream.

## Replay and summaries

```bash
uv run sixnimmt replay traces/first-run/MATCH_LOG.jsonl
uv run sixnimmt summarise traces/first-run/MATCH_LOG.jsonl
uv run sixnimmt summarise traces/first-run/MATCH_LOG.jsonl --manifest traces/first-run/manifest.json
```

Replace `MATCH_LOG.jsonl` with the manifest's filename. Replay folds events into
state and prints status, hand/play counts, scores, and winners. It does not
re-execute bot decisions or resume the match.

Summary reads the matching `.actions.jsonl` file and automatically uses
`manifest.json` beside the event log when present. It reports per-seat scores,
wins/ties, attempts/rejections, messages, and available decision latencies.
Direct-message copies are counted once per logical message. A manifest preserves
the distinction between a failed, forfeited, and abandoned match; without one,
summary reports the replay status and notes missing outcome context.

Python readers:

```python
from pathlib import Path

from sixnimmt.engine.replay import replay_events
from sixnimmt.persistence.sink import read_event_log


def replay_file(path: Path):
    return replay_events(read_event_log(path))
```

Replay reconstructs the undealt cards but does not promise their original order.
Compare the undealt remainder as a set/multiset or sorted list when checking
replayed state against live state. Legacy fixture logs preserve compatibility
with older selection-only action accounting; they are intentional test assets.

## Deterministic seeds

Arena match and bot seeds use the first eight bytes of SHA-256, interpreted as
an unsigned big-endian integer, over this UTF-8 string:

```text
sixnimmt-arena:{root_seed}:{domain}:{game_index}:{seat_index}
```

The domain is `match` or `bot`; indices are zero-based. Match seeds use the literal
`None` for the seat index. Per-hand shuffle seeds use the same digest conversion
over `{match_seed}:{hand_number}`, with hands numbered from one. Shuffling uses
Python's `random.Random` and an initially ordered deck from 1 through 104.

Independent bot seeds keep one seat's random choices from consuming another
seat's random stream. Reproducibility depends on the strategy, options, and
Python runtime as well as the seed. A standalone `run_match` trace is marked
non-reproducible because directly supplied instances have no registry promise.
Timestamps, generated run identifiers, and measured latency need not repeat.

## Crash recovery

The JSONL writer stages a batch in a `.pending` sidecar containing the previous
committed byte length. Readers ignore an unacknowledged tail; reopening the writer
truncates it back to the committed length. Event parsing tolerates an incomplete
final JSON line, but rejects corruption earlier in the log. Use the supplied
readers to preserve these recovery rules.

A caller-supplied `EventSink` remains the caller's responsibility to close.
When the runner creates its own sink, it closes it on completion.

## Model diagnostics

Model logs associate attempts using `decision_id` and unique `request_id` values.
Records include requests, responses/provider errors, parsing results, and repair
attempts. `request.payload` holds messages, tools, and generation settings;
`response.body` is a string containing the raw response body. It may include
provider-specific reasoning or fields outside the SDK schema.

An `action_parsed` or `batch_parsed` record means parsing succeeded, not that the
arena accepted the action. Consult action/event logs for acceptance. Late model
calls can append diagnostics after game logs close, and interrupted requests can
remain without response records.

Sources: [persistence](../src/sixnimmt/persistence/sink.py),
[manifest](../src/sixnimmt/persistence/manifest.py),
[replay](../src/sixnimmt/engine/replay.py), and
[analytics](../src/sixnimmt/analytics/summary.py).
