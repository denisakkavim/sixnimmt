# Traces and replay

All runs keep results and provenance in memory by default. Supply
`--output-dir` to save them, and add `--trace` for detailed logs in that directory's
`traces/` subdirectory:

```bash
uv run sixnimmt play --games 2 --output-dir runs/first-run --trace
```

`--trace` requires `--output-dir`; the directory must not already exist.
Without `--output-dir`, the CLI writes no files, including when `--json` prints
the full report. Python callers set
`RunSettings(recording=RecordingOptions(output_dir=Path(...), trace=True), ...)`
and call `application.run(settings)`. Fixed lineups, sampled schedules, and watched
games all use the same recording policy and evidence format. External seats need
workspaces during execution; saving a run retains them under its output directory.

## Files

Use result records or the trace manifest's filenames rather than guessing names.
Saved runs contain these artifacts:

| File | Contents |
| --- | --- |
| `manifest.json` | Run status, runtime provenance, and artifact references |
| `plan.json` | Complete planned schedule, frozen configuration, assignments, seeds, and declared analysis settings |
| `results.jsonl` | Compact returned outcomes, completed-hand scores, and measurements |
| `analysis.json.gz` | Full typed analysis in gzip-compressed JSON |
| `report.md` | Readable strategy tables and collapsed previews of exploratory results |
| `traces/manifest.json` | Trace filenames and returned outcomes for single-match summaries |
| `traces/<match_id>.jsonl` | Complete authoritative event history, including private/admin events |
| `traces/<match_id>.actions.jsonl` | Attempt records and decision measurements |
| `traces/<match_id>.model.jsonl` | Optional raw model requests, responses, and parsing diagnostics |
| `matches/<match_id>/seats/player_N/` | Private external-seat workspace and connection files where applicable |

Each JSONL line is one JSON object. Event and action writers can add
`player_display_names` labels to aid inspection; the readers reconstruct the
typed event/action models independently of those labels.

With `--output-dir` and without `--trace`, evidence and reports are still saved.
External workspaces may also be present; detailed event, action, and model logs
require tracing. The CLI writes both reports
after execution. Lower-level `run_plan(..., output_dir=...)` saves evidence only;
`application.run` also publishes reports and returns their actual paths.
`arena.artifacts.publish_analysis` supports separate publication after reanalysis.
Each derived file is atomically replaced, but the pair is not published as one
transaction; reports can be rebuilt from authoritative evidence.
The comparison guide includes a [Python example for reading
the compressed analysis](comparisons.md#python-execution-and-reanalysis).

Untraced runs retain per-seat decision counts and total decision time, plus total
game time. Individual decision durations are retained only with `--trace`; the
median and 95th-percentile decision time are unavailable without those samples.
Scores, completed hands, seeds, failures, and reanalysis remain available without
tracing. Older runs with individual timing samples remain readable.

Explicit schema version tags require the integer `1`; booleans, floats, strings,
and unsupported versions are rejected. Older trace entries that omit their own
version tag retain the version-1 default. Missing historical provenance detail
remains readable, while any supplied provenance version is validated.

The trace manifest's version-1 entries include `game_index`, `match_id`, `seed`, `outcome`,
`winners`, `ended_by`, `reason`, `log`, `actions`, `seat_stats`, and `stats_errors`.
The root manifest and plan hold the run's rules, configuration, and
identities. All JSON manifests and plans share an atomic writer: it writes and
syncs a temporary file, replaces the destination, and syncs the containing
directory where supported. This durability policy applies to authoritative
metadata; derived reports use atomic replacement without the per-write sync cost.
The trace index can exist before a match starts; cancelled setup produces no
event log. Returned outcomes refresh both compact results and trace metadata. A crashed
worker may leave a partial trace without a returned outcome; its planned job
remains visible in execution status.

The standalone `run_match` primitive can write event files directly to
`RunConfig.trace_dir`. Its version-1 manifest additionally includes `rules`,
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
from sixnimmt.persistence.manifest import read_manifest_entry
from sixnimmt.persistence.sink import read_event_log


def replay_file(path: Path):
    return replay_events(read_event_log(path))


def outcome_for_log(path: Path):
    return read_manifest_entry(path.parent / "manifest.json", path.name).outcome
```

Replay reconstructs the undealt cards but does not promise their original order.
Compare the undealt remainder as a set/multiset or sorted list when checking
replayed state against live state. Legacy fixture logs preserve compatibility
with older selection-only action accounting; they are intentional test assets.

## Deterministic seeds

Every run plan saves actual match and per-seat bot seeds in every job, along
with the versioned seed scheme. Changing concurrency or execution backend does
not change those assignments. Matched replacements record explicit shared-deal
relationships; equal seeds across different player counts do not mean equal
dealt states. See [planning](../src/sixnimmt/arena/planning.py).

Fixed and sampled jobs share the `arena-plan-v1` scheme. Their actual assignments
are authoritative; the root seed alone is not a replacement for the plan. Per-hand
shuffle seeds use the first eight bytes of SHA-256, interpreted as an unsigned
big-endian integer, over `{match_seed}:{hand_number}`, with hands numbered from
one. Shuffling uses Python's `random.Random` and an initially ordered deck from
1 through 104. Existing recorded fixtures retain their original seed provenance.

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
readers to preserve these recovery rules. `persistence.sink.read_jsonl(path, parse)`
provides the same recovery behavior for another typed JSON record parser, and
preserves that parser's result type.

Readers apply the sidecar's committed byte boundary before parsing records. A
final line can be discarded only when JSON decoding identifies an interrupted
string or an unexpected end of input, the line has no terminating newline, and
at least one earlier record is valid. A valid final record needs no newline.
Complete schema errors (including unknown event types), ambiguous syntax errors,
and invalid middle records raise `LogRecordError` with the path and physical line
number. An entirely unreadable file also raises.

Saved-run loading and in-memory analysis use the same evidence identity validator.
Every result must identify one planned job, with the planned match ID and exact
seat assignments. Status lists reject unknown or duplicate job IDs and conflicting
started/unstarted or completed/lost claims. A status cannot claim a completed job
without its committed result.

A committed result can precede the next status write. `load_run` reconciles such
results into started/completed status and derives remaining lost/unstarted jobs;
`analyse_run` also counts returned outcomes as started when using a stale in-memory
snapshot. Competitive scores still come only from finished outcomes. Non-JSON or
non-finite statistics and invalid call counts or durations are rejected as invalid
evidence rather than entering resource summaries.

A caller-supplied `EventSink` remains the caller's responsibility to close.
When the runner creates its own sink, it closes it on completion.

## Model diagnostics

Existing LLM adapter logs associate attempts using `decision_id` and unique `request_id` values.
Records include requests, responses/provider errors, parsing results, and repair
attempts. `request.payload` holds messages, tools, and generation settings;
`response.body` is a string containing the raw response body. It may include
provider-specific reasoning or fields outside the SDK schema.

An `action_parsed` or `batch_parsed` record means parsing succeeded, not that the
arena accepted the action. Consult action/event logs for acceptance. Late model
calls can append diagnostics after game logs close, and interrupted requests can
remain without response records.

Managed external harnesses use the same model-log file. When tracing is enabled,
each job writes `traces/<match_id>.model.jsonl`, alongside its event and action logs.
Records carry `player_id`, `display_name`, `client`, configured `model` and
`reasoning_effort`, `invocation`, `decision_id`, `view_id`, and a timestamp.
Worker records additionally carry the attempt number.

| Record type | Contents |
| --- | --- |
| `decision_request` | This seat's filtered offer and decision-specific proposal schema |
| `invocation_started`, `invocation_completed` | Managed process activity |
| `model_text`, `reasoning_summary`, `tool_activity`, `stderr` | Available CLI output while the process runs |
| `invocation_output` | Bounded completed output for diagnosis |
| `proposal` | Parsed final proposal, before broker or arena acceptance |
| `decision_explanation` | Optional short model explanation from completed managed output, still awaiting acceptance |
| `proposal_rejected`, `protocol_repair` | Validation error, previous proposal or bounded excerpt, and retry context |
| `delivery_cancelled` | Delivery stopped while waiting for the next offer; this does not reject a move already accepted |
| `invocation_failed` | Process failure with bounded stdout/stderr diagnostics |
| `cleanup_failed` | Process cleanup failure; an existing invocation error remains the reported cause of failure |

The generic command contract still requires exactly one proposal on stdout;
its stderr can supply live diagnostic text. Vendor stream records are never
submissions. A repair retains the same decision identifiers, schema, and work
deadline, while getting a new invocation number. Inspect the action log and
receipts to determine whether a proposal was accepted.

Text and reasoning records preserve `item_id` and carry `complete` to mark a
finished message. An empty text record can mark completion of text already
streamed; it is not a new message. Tool records preserve the tool item's ID and
expose `tool_name` and `status`, allowing a display to update an existing entry.
Completion of streamed tool input is separate from completion of tool execution.

Managed output may include an optional `explanation`. It is validated and removed
before the proposal reaches the broker, and consumes no action or notebook
budget. The corresponding explanation record is operator commentary; accepted
actions and rejected attempts remain determined by arena settlement. Completed
output is retained privately for diagnosis, including explanations that fail
validation.

When tracing is enabled, these diagnostics survive failed games and are saved
even when the operator uses `--quiet` or `--no-commentary`. Large output is bounded and truncation is
marked. Known credential values are redacted from managed records, but the
files remain privileged: they include a player's observation, notebook, and
potentially private commentary. Native terminal transcripts are not collected.
Streamed reasoning summaries are only those exposed by the client.

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
