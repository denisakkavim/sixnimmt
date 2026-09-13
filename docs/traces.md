# Traces and replay

Arena comparisons keep results and provenance in memory by default. Supply
`--output-dir` to save them, and add `--trace` for detailed logs in that directory's
`traces/` subdirectory:

```bash
uv run sixnimmt arena --games 2 --output-dir runs/first-run --trace
```

`--trace` requires `--output-dir`; the directory must not already exist.
Without `--output-dir`, the CLI writes no files, including when `--json` prints
the full report. Python comparisons use
`run_plan(plan, output_dir=Path(...), trace=True)`. The lower-level `run_match`
and fixed-lineup `run_arena` APIs retain their `RunConfig(trace_dir=Path(...))`
option. Without tracing, `run_arena` returns fixed-seat aggregates, while
`run_match` returns its individual result with events.

## Files

Use result records or the trace manifest's filenames rather than guessing names.
Saved comparison runs contain these artifacts:

| File | Contents |
| --- | --- |
| `manifest.json` | Run status, runtime provenance, and artifact references |
| `plan.json` | Complete planned schedule, frozen configuration, assignments, seeds, and declared analysis settings |
| `results.jsonl` | Compact returned outcomes, completed-hand scores, and measurements |
| `analysis.json.gz` | Full typed analysis in gzip-compressed JSON |
| `report.md` | Readable strategy tables and collapsed previews of exploratory results |
| `traces/manifest.json` | Trace filenames and returned outcomes for single-match summaries |
| `traces/<match>.jsonl` | Complete authoritative event history, including private/admin events |
| `traces/<match>.actions.jsonl` | Attempt records and decision measurements |
| `traces/<match>.model.jsonl` | Optional raw model requests, responses, and parsing diagnostics |

Each JSONL line is one JSON object. Event and action writers can add
`player_display_names` labels to aid inspection; the readers reconstruct the
typed event/action models independently of those labels.

With `--output-dir` and without `--trace`, the output contains only `plan.json`, `results.jsonl`,
`manifest.json`, `report.md`, and `analysis.json.gz`. The CLI writes both reports
after execution. The comparison guide includes a [Python example for reading
the compressed analysis](comparisons.md#python-execution-and-reanalysis).

The trace manifest's version-1 entries include `game_index`, `match_id`, `seed`, `outcome`,
`winners`, `ended_by`, `reason`, `log`, `actions`, `seat_stats`, and `stats_errors`.
The root manifest and plan hold the comparison's rules, configuration, and
identities. Manifests are published through a temporary file and rename.
Returned outcomes refresh both compact results and trace metadata. A crashed
worker may leave a partial trace without a returned outcome; its planned job
remains visible in execution status.

The lower-level Python runners write event files directly in their requested
trace directory. Their version-1 manifest additionally includes `rules`,
`protocol`, `run_config`, `seats`, `seed`, run counts, and `reproducible`.

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
uv run sixnimmt replay runs/first-run/traces/MATCH_LOG.jsonl
uv run sixnimmt summarise runs/first-run/traces/MATCH_LOG.jsonl
uv run sixnimmt summarise runs/first-run/traces/MATCH_LOG.jsonl --manifest runs/first-run/traces/manifest.json
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

Comparison plans save actual match and per-seat bot seeds in every job, along
with the versioned seed scheme. Changing concurrency or execution backend does
not change those assignments. Matched replacements record explicit shared-deal
relationships; equal seeds across different player counts do not mean equal
dealt states. See [planning](../src/sixnimmt/arena/planning.py).

The fixed-lineup `run_arena` API preserves its existing derivation: match and
bot seeds use the first eight bytes of SHA-256, interpreted as
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

Readers apply the sidecar's committed byte boundary before parsing records. A
final line can be discarded only when JSON decoding identifies an interrupted
string or an unexpected end of input, the line has no terminating newline, and
at least one earlier record is valid. A valid final record needs no newline.
Complete schema errors (including unknown event types), ambiguous syntax errors,
and invalid middle records raise `LogRecordError` with the path and physical line
number. An entirely unreadable file also raises.


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

## Event payload contracts

Each event's `data` has a concrete `TypedDict` schema validated by Pydantic when
an event is constructed or read. Required fields and their types are checked
before replay or view folding. Payloads remain dictionaries, and the serialized
envelope and `data` nesting are unchanged. Narrow on `event.type` before accessing
its event-specific keys. Payload keys outside the declared schema are rejected;
opaque agent metadata uses JSON values.

Public match creation rejects agent metadata, and public abandonment contains no
diagnostics. Privileged abandonment requires its outcome, actor, and reason fields.
Top-level annotations such as `player_display_names` remain readable and are not
part of authoritative payloads. Existing unversioned fixtures remain supported;
older logs without action-count events retain their documented counting behavior.
Synthetic events in Python must now provide their complete payloads.
