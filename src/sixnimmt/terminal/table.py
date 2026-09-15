"""Live public gameplay with a separate, explicitly privileged operator pane."""

import json
import re
import unicodedata
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Event as ThreadEvent
from threading import Lock, Thread
from time import monotonic
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sixnimmt.engine.audience import Viewer, visible_events
from sixnimmt.engine.events import Event
from sixnimmt.engine.fold import ViewFolder
from sixnimmt.engine.state import MatchState
from sixnimmt.engine.views import MatchView, MessageView, OpponentView, ViewRole
from sixnimmt.terminal.board import render_board

_MAX_FRAMES = 32
_MAX_OPERATOR_LINES = 8
_MAX_TEXT = 1200
_FRAME_SECONDS = 0.22
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])")
_FRAME_EVENTS = frozenset({
    "match_created",
    "hand_started",
    "rows_initialised",
    "play_started",
    "selection_registered",
    "selection_cleared",
    "player_committed",
    "player_uncommitted",
    "message_sent",
    "cards_revealed",
    "card_placed",
    "row_taken",
    "row_choice_required",
    "hand_ended",
    "match_ended",
    "match_abandoned",
})
_DETAIL_TYPES = frozenset({"model_text", "reasoning_summary", "tool_activity", "stderr", "simulation_evaluation"})
_FAILURE_TYPES = frozenset({"invocation_failed", "proposal_rejected", "protocol_repair"})
_ACTIVITY_TYPES = (
    _DETAIL_TYPES
    | _FAILURE_TYPES
    | {"decision_started", "decision_finished", "match_finished", "invocation_started", "invocation_completed"}
)


def _safe_text(value: object, limit: int = _MAX_TEXT) -> str:
    """Treat model output as text, removing terminal controls and bidi overrides."""
    text = _ANSI.sub("", str(value)[: limit * 4])
    clean = "".join(char for char in text if char == "\n" or not unicodedata.category(char).startswith("C"))
    if len(clean) > limit:
        return clean[:limit] + "…"
    return clean


def _name(view: MatchView, player_id: str) -> str:
    player = next((player for player in view.players if player.player_id == player_id), None)
    return _safe_text(player.display_name if player is not None else player_id, 80).replace("\n", " ")


@dataclass(frozen=True)
class _Frame:
    view: MatchView
    caption: str = "Waiting for the table to start"
    row: int | None = None
    card: int | None = None
    captured: bool = False


@dataclass(frozen=True)
class _SeatActivity:
    name: str
    status: str
    started: float
    deadline: datetime | None = None
    finished: float | None = None


@dataclass(frozen=True)
class _OperatorLine:
    player_id: str
    name: str
    kind: str
    text: str


def _event_frame(view: MatchView, event: Event) -> _Frame:  # noqa: C901 - exhaustive public event captions
    match event.type:
        case "row_taken":
            data = event.data
            reason = "Sixth card" if data["reason"] == "sixth_card" else "Too low"
            cards = ", ".join(map(str, data["captured"]))
            caption = f"{reason}: {_name(view, data['player_id'])} takes Row {data['row'] + 1} ({cards}), +{data['heads']} bull heads"
            return _Frame(view, caption, row=data["row"], captured=True)
        case "card_placed":
            data = event.data
            return _Frame(view, f"Card {data['card']} joins Row {data['row'] + 1}", row=data["row"], card=data["card"])
        case "cards_revealed":
            cards = sorted(event.data["selections"].items(), key=lambda entry: entry[1])
            caption = "Reveal: " + " · ".join(f"{_name(view, player)} [{card}]" for player, card in cards)
        case "row_choice_required":
            caption = f"{_name(view, event.data['player_id'])} must choose a row for [{event.data['card']}]"
        case "message_sent":
            caption = f"{_name(view, event.data['from'])}: {_safe_text(event.data['body'])}"
        case "selection_registered":
            caption = f"{_name(view, event.data['player_id'])} selected a face-down card"
        case "player_committed":
            caption = f"{_name(view, event.data['player_id'])} is ready"
        case "match_ended":
            winners = ", ".join(_name(view, player) for player in event.data["winners"])
            caption = f"Match finished · Winner{'s' if len(event.data['winners']) != 1 else ''}: {winners}"
        case "match_abandoned":
            caption = "Match abandoned"
        case "hand_ended":
            caption = f"Hand {view.hand_number} finished · scores banked"
        case "play_started":
            caption = "Choose cards · selections remain face-down until everyone is ready"
        case "rows_initialised":
            caption = "Four rows dealt"
        case _:
            caption = event.type.value.replace("_", " ").capitalize()
    return _Frame(view, caption)


def _board(frame: _Frame) -> Table:
    rows = tuple(row.cards for row in frame.view.rows)
    return render_board(rows, highlighted_row=frame.row, highlighted_card=frame.card, captured=frame.captured)


def _seat_card(view: MatchView, player: OpponentView) -> str:
    if len(view.play_history) > 0:
        play = view.play_history[-1]
        if play.hand_number == view.hand_number and play.play_number == view.play_number:
            revealed = next((card for card in play.cards if card.player_id == player.player_id), None)
            if revealed is not None:
                return f"[{revealed.card:3}]" + (" placed" if revealed.row_index is not None else " revealed")
    if player.committed:
        return "[ ? ] ready"
    if player.has_selection:
        return "[ ? ] selected"
    return "[ · ]"


def _activity_text(activity: _SeatActivity | None, now: float) -> Text:
    if activity is None:
        return Text("waiting", style="dim")
    end = now if activity.finished is None else activity.finished
    seconds = max(0.0, end - activity.started)
    active = activity.status in ("deciding", "retrying")
    style = "yellow" if active else "red" if activity.status in ("failed", "rejected") else "green"
    label = f"{activity.status} · {seconds:.1f}s"
    if active and activity.deadline is not None:
        remaining = max(0.0, (activity.deadline - datetime.now(UTC)).total_seconds())
        label += f" · {remaining:.0f}s left"
    return Text(label, style=style)


def _players(frame: _Frame, activities: dict[str, _SeatActivity], now: float) -> Table:
    table = Table.grid(padding=(0, 2), expand=True)
    for _ in range(4):
        table.add_column()
    table.add_row(
        Text("Player", style="dim"),
        Text("Card", style="dim"),
        Text("Score", style="dim"),
        Text("Activity", style="dim"),
    )
    for player in frame.view.players:
        score = player.total_score + player.score_this_hand
        table.add_row(
            Text(_name(frame.view, player.player_id), style="bold"),
            Text(_seat_card(frame.view, player)),
            Text(f"{score} ({player.total_score}+{player.score_this_hand})"),
            _activity_text(activities.get(player.player_id), now),
        )
    return table


def _public_messages(view: MatchView) -> list[Text]:
    messages = [entry for entry in view.message_history if isinstance(entry.message, MessageView)]
    return [
        Text(f"{_name(view, entry.message.from_player)}: {_safe_text(entry.message.body, 240)}")
        for entry in messages[-3:]
        if isinstance(entry.message, MessageView)
    ]


def _operator_panel(lines: tuple[_OperatorLine, ...], width: int) -> Panel:
    rows = [Text(f"{line.name} · {line.kind.replace('_', ' ')}\n{line.text[-360:]}") for line in lines[-3:]]
    if len(rows) == 0:
        rows = [Text("Waiting for model commentary or strategy evaluations…", style="dim")]
    return Panel(
        Group(*rows), title="Operator commentary · may include private information", border_style="magenta", width=width
    )


def _render_frame(
    frame: _Frame,
    activities: dict[str, _SeatActivity],
    lines: tuple[_OperatorLine, ...],
    terminal: str,
    *,
    width: int,
    now: float,
    commentary: bool,
) -> Group:
    view = frame.view
    title = f"6 nimmt! · Hand {view.hand_number} · Play {view.play_number} · {view.phase.value.replace('_', ' ')}"
    contents = [
        _players(frame, activities, now),
        Text("Scores: total (banked + this hand) · lowest wins", style="dim"),
        Text(),
        _board(frame),
        Text("Four rows · five slots · ^ bull heads", style="dim"),
        Text(frame.caption, style="bold red" if frame.captured else "bold cyan"),
    ]
    messages = _public_messages(view)
    if len(messages) > 0:
        contents.extend([Text("Public table messages", style="bold"), *messages])
    board = Panel(Group(*contents), title=Text(title), border_style="red" if frame.captured else "cyan", width=width)
    panels = [board]
    if terminal != "":
        panels.append(Panel(Text(terminal), title="Operator status", border_style="red", width=width))
    if commentary:
        panels.append(_operator_panel(lines, width))
    return Group(*panels)


def _deadline(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError:
        return None
    return deadline.replace(tzinfo=UTC) if deadline.tzinfo is None else deadline


def _record_text(record: dict[str, Any]) -> str:
    for key in ("text", "message", "reason", "error"):
        value = record.get(key)
        if isinstance(value, dict):
            value = value.get("message")
        if value is not None:
            return _safe_text(value)
    return ""


def _llm_response_message(record: dict[str, Any]) -> dict[str, Any]:
    body = record.get("body")
    if not isinstance(body, str) or len(body) > 1_048_576:
        return {}
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(payload, dict):
        return {}
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) == 0 or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get("message")
    return message if isinstance(message, dict) else {}


def _activity_records(record: dict[str, Any]) -> list[dict[str, Any]]:
    kind = record.get("type")
    if isinstance(kind, str) and kind in _ACTIVITY_TYPES:
        return [record]
    if record.get("kind") != "response":
        return []
    message = _llm_response_message(record)
    records = []
    for field, kind in (
        ("content", "model_text"),
        ("reasoning_content", "reasoning_summary"),
        ("reasoning", "reasoning_summary"),
    ):
        text = message.get(field)
        if isinstance(text, str) and text != "":
            records.append({**record, "type": kind, "text": text})
    calls = message.get("tool_calls", [])
    if isinstance(calls, list):
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                records.append({**record, "type": "tool_activity", "text": function["name"]})
    return records


def _candidate_text(record: dict[str, Any]) -> str:
    values = record.get("candidate_values")
    if not isinstance(values, dict) or len(values) == 0:
        return _safe_text(record.get("evaluation", "No candidate estimates"))
    entries = [(str(card), float(value)) for card, value in values.items() if isinstance(value, (float, int))]
    entries.sort(key=lambda entry: (entry[1], entry[0]))
    return "Candidate values (lower is better): " + ", ".join(f"[{card}] {value:.2f}" for card, value in entries[:10])


class PublicTableDisplay:
    """Fold public events only; keep optional operator activity out of the game view.

    The context manager enables queued delivery, so rendering and terminal writes
    happen outside match callbacks. Direct construction retains plain reporting.
    """

    def __init__(self, quiet: bool = False, report: Callable[[str], None] = print, *, commentary: bool = True) -> None:
        self.folder = ViewFolder(Viewer(ViewRole.PUBLIC_SPECTATOR))
        self.quiet = quiet
        self.report = report
        self.commentary = commentary
        self._lock = Lock()
        self._pending: deque[_Frame] = deque(maxlen=_MAX_FRAMES)
        self._current = _Frame(self.folder.view())
        self._latest = self._current
        self._activities: dict[str, _SeatActivity] = {}
        self._lines: deque[_OperatorLine] = deque(maxlen=_MAX_OPERATOR_LINES)
        self._plain_activity: deque[str] = deque(maxlen=32)
        self._terminal = ""
        self._queued = False
        self._next_frame = 0.0
        self._closing = False
        self._wake = ThreadEvent()
        self._drained = ThreadEvent()

    def observe(self, state: MatchState, events: tuple[Event, ...]) -> None:
        # Authoritative state is deliberately unused, including its final values.
        public = visible_events(events, Viewer(ViewRole.PUBLIC_SPECTATOR))
        output: list[str] = []
        with self._lock:
            for event in public:
                self.folder.apply((event,))
                if event.type not in _FRAME_EVENTS:
                    continue
                frame = _event_frame(self.folder.view(), event)
                self._latest = frame
                if self.quiet:
                    continue
                if self._queued:
                    self._pending.append(frame)
                else:
                    self._current = frame
                    output.append(self._plain_frame(frame))
            self._wake.set()
        for message in output:
            self.report(message)

    def activity(self, record: dict[str, Any]) -> None:
        if self.quiet:
            return
        if not self.commentary and record.get("kind") == "response":
            return
        for normalized in _activity_records(record):
            self._activity(normalized)

    def _activity(self, record: dict[str, Any]) -> None:
        kind = str(record.get("type", ""))
        if kind in _DETAIL_TYPES and not self.commentary:
            return
        player_id = _safe_text(record.get("player_id", "table"), 80)
        name = _safe_text(record.get("display_name", player_id), 80).replace("\n", " ")
        text = _candidate_text(record) if kind == "simulation_evaluation" else _record_text(record)
        with self._lock:
            self._update_activity(record, kind, player_id, name, text)
            if kind in _DETAIL_TYPES or kind in _FAILURE_TYPES:
                self._add_operator_line(_OperatorLine(player_id, name, kind, text))
            label = f"{name} · {kind.replace('_', ' ')}"
            status = record.get("status") if kind == "decision_finished" else record.get("outcome")
            if status is not None:
                label += f" · {_safe_text(status, 80)}"
            if text != "":
                label += f": {text}"
            if kind in _DETAIL_TYPES:
                label = "Operator commentary · " + label
            if self._queued:
                self._plain_activity.append(label)
                self._wake.set()
                return
        self.report(label)

    def _update_activity(self, record: dict[str, Any], kind: str, player_id: str, name: str, text: str) -> None:
        self._update_terminal(record, kind, name, text)
        now = monotonic()
        if kind == "decision_started":
            self._activities[player_id] = _SeatActivity(name, "deciding", now, _deadline(record.get("deadline")))
            return
        activity = self._activities.get(player_id)
        if activity is not None:
            if kind == "invocation_started":
                deadline = _deadline(record.get("deadline"))
                if deadline is not None:
                    self._activities[player_id] = replace(activity, deadline=deadline)
            elif kind in ("protocol_repair", "proposal_rejected"):
                self._activities[player_id] = replace(activity, status="retrying", finished=None)
            elif kind == "decision_finished":
                status = _safe_text(record.get("status", "finished"), 40)
                self._activities[player_id] = replace(activity, status=status, finished=now)
            elif kind == "invocation_failed":
                self._activities[player_id] = replace(activity, status="failed", finished=now)

    def _update_terminal(self, record: dict[str, Any], kind: str, name: str, text: str) -> None:
        if kind == "match_finished":
            outcome = _safe_text(record.get("outcome", "finished"), 80)
            self._terminal = f"Match {outcome}" + (f": {text}" if text != "" else "")
        elif kind in ("invocation_failed", "proposal_rejected"):
            self._terminal = f"{name}: {text}" if text != "" else f"{name}: {kind.replace('_', ' ')}"
        elif kind == "decision_finished" and record.get("status") == "accepted":
            self._terminal = ""

    def _add_operator_line(self, line: _OperatorLine) -> None:
        if line.text == "" or not self.commentary:
            return
        if len(self._lines) > 0 and line.kind in ("model_text", "reasoning_summary"):
            previous = self._lines[-1]
            if previous.player_id == line.player_id and previous.kind == line.kind:
                self._lines[-1] = replace(previous, text=(previous.text + line.text)[-_MAX_TEXT:])
                return
        self._lines.append(line)

    @staticmethod
    def _plain_frame(frame: _Frame) -> str:
        view = frame.view
        rows = " | ".join(f"{row.index + 1}: " + ",".join(map(str, row.cards)) for row in view.rows)
        scores = ", ".join(
            f"{_name(view, player.player_id)}={player.total_score + player.score_this_hand} "
            f"({player.total_score}+{player.score_this_hand})"
            for player in view.players
        )
        return f"Hand {view.hand_number}, play {view.play_number}, {view.phase.value}\n{frame.caption}\nRows {rows}\nScores {scores}"

    def render(self, width: int = 100) -> Group:
        now = monotonic()
        with self._lock:
            if len(self._pending) > 0 and now >= self._next_frame:
                self._current = self._pending.popleft()
                # Catch up when bots are fast; never block a decision for a frame.
                interval = 0.0 if self._closing else min(_FRAME_SECONDS, 2.0 / max(1, len(self._pending)))
                self._next_frame = now + interval
            if self._closing and len(self._pending) == 0:
                self._drained.set()
            frame = self._current
            activities = dict(self._activities)
            lines = tuple(self._lines)
            terminal = self._terminal
        return _render_frame(frame, activities, lines, terminal, width=width, now=now, commentary=self.commentary)

    def _plain_worker(self) -> None:
        while True:
            self._wake.wait(0.1)
            with self._lock:
                frames = tuple(self._pending)
                messages = tuple(self._plain_activity)
                self._pending.clear()
                self._plain_activity.clear()
                self._wake.clear()
                closing = self._closing
            for frame in frames:
                self.report(self._plain_frame(frame))
            for message in messages:
                self.report(message)
            if closing:
                self._drained.set()
                return

    def _begin_close(self) -> None:
        with self._lock:
            self._closing = True
            self._wake.set()

    def _live_worker(self, console: Console) -> None:
        # Waiting-room prompts precede the first event, so they never compete
        # with a live region for the terminal or its cursor.
        while True:
            self._wake.wait(0.1)
            with self._lock:
                self._wake.clear()
                has_frames = len(self._pending) > 0
                closing = self._closing
                messages = tuple(self._plain_activity) if closing else ()
            if has_frames:
                break
            if closing:
                for message in messages:
                    self.report(message)
                self._drained.set()
                return
        with Live(console=console, get_renderable=lambda: self.render(console.width), auto_refresh=False) as live:
            closing_at: float | None = None
            while True:
                live.refresh()
                with self._lock:
                    self._wake.clear()
                    closing = self._closing
                if closing:
                    if closing_at is None:
                        closing_at = monotonic()
                    if self._drained.is_set() or monotonic() - closing_at >= 3.0:
                        break
                self._wake.wait(1 / 12)
            self._finish()
            live.refresh()

    def _finish(self) -> None:
        with self._lock:
            self._current = self._latest
            self._pending.clear()
            self._drained.set()


@contextmanager
def table_display(
    *, enabled: bool = True, quiet: bool = False, commentary: bool = True, report: Callable[[str], None] = print
) -> Iterator[PublicTableDisplay]:
    """Animate actual events on TTYs, print plain updates otherwise, and restore the cursor."""
    display = PublicTableDisplay(quiet=quiet, report=report, commentary=commentary)
    if quiet:
        yield display
        return
    display._queued = True
    console = Console(highlight=False)
    animated = enabled and console.is_terminal and not console.is_dumb_terminal
    worker = Thread(
        target=display._live_worker if animated else display._plain_worker,
        args=(console,) if animated else (),
        name="sixnimmt-table-output",
        daemon=True,
    )
    worker.start()
    try:
        yield display
    finally:
        display._begin_close()
        worker.join(timeout=4.0)
