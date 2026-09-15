"""Bounded decision commentary, separated from the public gameplay projection."""

import json
import math
import re
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markdown import Markdown
from rich.panel import Panel
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

_MAX_DECISIONS = 24
_MAX_ITEMS = 12
_MAX_TEXT = 16_000
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])")
_TEXT_TYPES = frozenset({"model_text", "reasoning_summary", "decision_explanation", "stderr"})


def safe_text(value: object, limit: int = _MAX_TEXT) -> str:
    """Remove terminal controls and bidi overrides without interpreting Rich markup."""
    text = _ANSI.sub("", str(value)[: limit * 4])
    clean = "".join(char for char in text if char == "\n" or not unicodedata.category(char).startswith("C"))
    return clean[:limit] + ("… [text truncated]" if len(clean) > limit else "")


def _label(value: object, limit: int = 100) -> str:
    return safe_text(value, limit).replace("\n", " ")


@dataclass(frozen=True)
class Message:
    kind: str
    invocation: str
    item_id: str
    text: str
    complete: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class Tool:
    invocation: str
    item_id: str
    name: str
    status: str


@dataclass(frozen=True)
class Candidates:
    values: tuple[tuple[str, float], ...]
    chosen: str | None = None
    objective: str = "value"
    horizon: str = ""
    samples: int | None = None
    probability: bool = False


@dataclass(frozen=True)
class DecisionCommentary:
    player_id: str
    name: str
    number: str
    hand: str
    play: str
    phase: str
    model: str
    effort: str
    invocation: str
    attempt: str
    status: str
    choices: tuple[str, ...]
    choice_status: str
    explanation: str
    messages: tuple[Message, ...]
    tools: tuple[Tool, ...]
    candidates: Candidates | None
    diagnostic: str


@dataclass
class _Decision:
    player_id: str
    name: str
    number: str = ""
    view_id: str = ""
    hand: str = ""
    play: str = ""
    phase: str = ""
    model: str = ""
    effort: str = ""
    invocation: str = ""
    attempt: str = ""
    status: str = "deciding"
    choices: tuple[str, ...] = ()
    choice_status: str = ""
    explanation: str = ""
    messages: OrderedDict[tuple[str, str, str], Message] = field(default_factory=OrderedDict)
    tools: OrderedDict[tuple[str, str], Tool] = field(default_factory=OrderedDict)
    candidates: Candidates | None = None
    diagnostic: str = ""
    archived: bool = False
    settled: bool = False

    def snapshot(self) -> DecisionCommentary:
        return DecisionCommentary(
            self.player_id,
            self.name,
            self.number,
            self.hand,
            self.play,
            self.phase,
            self.model,
            self.effort,
            self.invocation,
            self.attempt,
            self.status,
            self.choices,
            self.choice_status,
            self.explanation,
            tuple(self.messages.values()),
            tuple(self.tools.values()),
            self.candidates,
            self.diagnostic,
        )


@dataclass(frozen=True)
class CommentarySnapshot:
    current: DecisionCommentary | None
    previous: DecisionCommentary | None = None


def _actions(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    summaries = []
    for action in value[:8]:
        if not isinstance(action, dict):
            continue
        kind = action.get("type")
        if kind == "select_card" and type(action.get("card")) is int:
            summaries.append(f"select card {action['card']}")
        elif kind == "choose_row" and type(action.get("row_index")) is int:
            summaries.append(f"take row {action['row_index'] + 1}")
        elif kind in ("commit", "uncommit"):
            summaries.append(str(kind))
        elif kind == "send_message":
            visibility = "public" if action.get("visibility") == "table" else "private"
            summaries.append(f"send {visibility} message")
    return tuple(summaries)


def _diagnostic(record: dict[str, Any]) -> str:
    for key in ("text", "message", "reason", "error"):
        value = record.get(key)
        if isinstance(value, dict):
            value = value.get("message")
        if value is not None:
            return safe_text(value, 1000)
    return ""


def _objective(value: object) -> tuple[str, bool]:
    if not isinstance(value, dict):
        return "Candidate value", False
    kind = value.get("kind")
    if kind == "mean":
        return "Expected bull heads", False
    if kind == "pickup_probability":
        return "Chance of taking cards", True
    if kind == "threshold_exceedance":
        return f"Chance of exceeding {_label(value.get('threshold', '?'))} bull heads", True
    if kind == "upper_tail":
        fraction = value.get("tail_fraction")
        if isinstance(fraction, (int, float)) and math.isfinite(fraction):
            return f"Bull heads in worst {fraction:.0%} of outcomes", False
    return "Candidate value", False


def _candidates(record: dict[str, Any]) -> Candidates | None:
    raw = record.get("candidate_values")
    if not isinstance(raw, dict):
        return None
    values = [
        (_label(card, 8), float(value))
        for card, value in raw.items()
        if isinstance(value, (int, float)) and math.isfinite(value)
    ]
    values.sort(key=lambda item: (item[1], item[0]))
    objective, probability = _objective(record.get("objective"))
    chosen = record.get("chosen_card")
    horizon = record.get("horizon_plays")
    samples = record.get("sample_count")
    return Candidates(
        tuple(values[:104]),
        str(chosen) if type(chosen) is int else None,
        objective,
        str(horizon) if type(horizon) is int else "",
        samples if type(samples) is int else None,
        probability,
    )


class CommentaryBook:
    """Group observations without retaining offers, credentials or notebook contents.

    The caller owns synchronization. Snapshots and completed entries are immutable
    so terminal rendering can happen after releasing the match callback's lock.
    """

    def __init__(self) -> None:
        self._decisions: OrderedDict[tuple[str, str], _Decision] = OrderedDict()
        self._current: dict[str, tuple[str, str]] = {}
        self._external: OrderedDict[tuple[str, str], tuple[str, str]] = OrderedDict()
        self._serial = 0
        self._focus: tuple[str, str] | None = None
        self._completed: deque[DecisionCommentary] = deque(maxlen=_MAX_DECISIONS)
        self._previous: DecisionCommentary | None = None

    def observe(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "match_finished":
            self._archive_finished(final=True)
            return
        if kind == "decision_started":
            self._archive_finished()
        decision = self._decision(record)
        if decision is None:
            return
        if decision.settled and kind not in (
            "model_text",
            "reasoning_summary",
            "stderr",
            "tool_activity",
            "simulation_evaluation",
            "invocation_completed",
            "delivery_cancelled",
        ):
            return
        previous_invocation = decision.invocation
        if not decision.settled:
            self._context(decision, record)
            self._begin_invocation(decision, previous_invocation)
        invocation = record.get("invocation")
        if (
            invocation is not None
            and str(invocation) != decision.invocation
            and kind
            in ("proposal", "decision_explanation", "protocol_repair", "proposal_rejected", "invocation_failed")
        ):
            return
        self._update(decision, record)

    def _update(self, decision: _Decision, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind in _TEXT_TYPES:
            self._message(decision, record)
        elif kind == "tool_activity":
            self._tool(decision, record)
        elif kind == "proposal":
            self._proposal(decision, record)
        elif kind == "decision_finished":
            self._settle(decision, record)
        elif kind == "simulation_evaluation":
            decision.candidates = _candidates(record)
        elif kind in ("protocol_repair", "proposal_rejected", "invocation_failed"):
            self._failure(decision, record)
        elif kind == "invocation_completed":
            self._complete_messages(decision, str(record.get("invocation", "")))
        elif kind == "delivery_cancelled":
            decision.diagnostic = "Delivery stopped: " + _diagnostic(record)

    def _lookup_key(self, player: str, record: dict[str, Any]) -> tuple[str, str] | None:
        number = record.get("decision_number")
        if number is not None:
            return (player, f"arena:{number}")
        external = record.get("decision_id")
        if external is not None:
            known = self._external.get((player, str(external)))
            if known is not None:
                return known
        if record.get("type") == "decision_started":
            return None
        view = record.get("view_id")
        if isinstance(view, str) and view != "":
            for key, decision in reversed(self._decisions.items()):
                if decision.player_id == player and decision.view_id == view:
                    return key
        return self._current.get(player)

    def _decision(self, record: dict[str, Any]) -> _Decision | None:
        player = _label(record.get("player_id", "table"))
        kind = record.get("type")
        external = record.get("decision_id")
        external_key = (player, str(external)) if external is not None else None
        key = self._lookup_key(player, record)
        existing = self._decisions.get(key) if key is not None else None
        view = record.get("view_id")
        if existing is not None and isinstance(view, str) and existing.view_id not in ("", view):
            return None
        if key is None:
            self._serial += 1
            key = (player, f"local:{self._serial}")
        if key not in self._decisions:
            self._decisions[key] = _Decision(player, _label(record.get("display_name", player)))
            self._trim()
        if external_key is not None:
            self._external[external_key] = key
            self._external.move_to_end(external_key)
            while len(self._external) > _MAX_DECISIONS * _MAX_ITEMS:
                self._external.popitem(last=False)
        if kind == "decision_started" or player not in self._current:
            self._current[player] = key
            self._focus = key
        elif self._focus is None:
            self._focus = key
        return self._decisions[key]

    def _trim(self) -> None:
        while len(self._decisions) > _MAX_DECISIONS:
            oldest, _ = self._decisions.popitem(last=False)
            self._external = OrderedDict((external, key) for external, key in self._external.items() if key != oldest)
            self._current = {player: key for player, key in self._current.items() if key != oldest}

    @staticmethod
    def _context(decision: _Decision, record: dict[str, Any]) -> None:
        fields = {
            "display_name": "name",
            "decision_number": "number",
            "view_id": "view_id",
            "hand_number": "hand",
            "play_number": "play",
            "phase": "phase",
            "model": "model",
            "reasoning_effort": "effort",
            "invocation": "invocation",
            "attempt": "attempt",
        }
        for source, target in fields.items():
            value = record.get(source)
            if value is not None:
                label = _label(value)
                previous = getattr(decision, target)
                if (
                    target in ("invocation", "attempt")
                    and label.isdigit()
                    and previous.isdigit()
                    and int(label) < int(previous)
                ):
                    continue
                setattr(decision, target, label)

    @staticmethod
    def _begin_invocation(decision: _Decision, previous: str) -> None:
        if previous == "" or previous == decision.invocation:
            return
        decision.choices = ()
        decision.choice_status = ""
        decision.explanation = ""

    @staticmethod
    def _message(decision: _Decision, record: dict[str, Any]) -> None:
        kind = str(record.get("type", "model_text"))
        text = record.get("text")
        if not isinstance(text, str):
            return
        if kind == "decision_explanation":
            decision.explanation = safe_text(text, 1000)
            return
        invocation = str(record.get("invocation", ""))
        item = str(record.get("item_id", f"{kind}:default"))
        key = (invocation, item, kind)
        previous = decision.messages.get(key)
        content = safe_text(text)
        if previous is not None and record.get("delta") is True:
            content = previous.text + content
        truncated = len(content) > _MAX_TEXT or (previous is not None and previous.truncated)
        decision.messages[key] = Message(
            kind, invocation, item, content[:_MAX_TEXT], record.get("complete") is True, truncated
        )
        while len(decision.messages) > _MAX_ITEMS:
            decision.messages.popitem(last=False)

    @staticmethod
    def _tool(decision: _Decision, record: dict[str, Any]) -> None:
        invocation = str(record.get("invocation", ""))
        text = str(record.get("text", "tool"))
        name, _, fallback_status = text.rpartition(": ")
        if name == "":
            name = text
        tool_name = _label(record.get("tool_name", name))
        status = _label(record.get("status", fallback_status if fallback_status != "" else "running"), 30)
        item = str(record.get("item_id", tool_name))
        key = (invocation, item)
        decision.tools[key] = Tool(invocation, item, tool_name, status)
        while len(decision.tools) > _MAX_ITEMS:
            decision.tools.popitem(last=False)

    @staticmethod
    def _proposal(decision: _Decision, record: dict[str, Any]) -> None:
        proposal = record.get("proposal")
        if isinstance(proposal, dict):
            decision.choices = _actions(proposal.get("actions"))
            decision.choice_status = "Proposed"

    @staticmethod
    def _settle(decision: _Decision, record: dict[str, Any]) -> None:
        decision.settled = True
        decision.status = _label(record.get("status", "finished"), 30)
        accepted = decision.status == "accepted"
        actions = record.get("actions") if accepted else record.get("attempted_actions")
        choices = _actions(actions)
        if accepted or len(choices) > 0:
            decision.choices = choices
        decision.choice_status = decision.status.capitalize()
        decision.diagnostic = _diagnostic(record)

    @staticmethod
    def _failure(decision: _Decision, record: dict[str, Any]) -> None:
        kind = record.get("type")
        retrying = kind == "protocol_repair" or (
            kind == "proposal_rejected" and record.get("repair_will_follow") is True
        )
        decision.status = "retrying" if retrying else "failed"
        if kind == "proposal_rejected":
            decision.choice_status = "Rejected"
        decision.diagnostic = _diagnostic(record)

    @staticmethod
    def _complete_messages(decision: _Decision, invocation: str) -> None:
        for key, message in tuple(decision.messages.items()):
            if message.invocation == invocation:
                decision.messages[key] = Message(
                    message.kind, message.invocation, message.item_id, message.text, True, message.truncated
                )

    def _archive_finished(self, *, final: bool = False) -> None:
        for decision in self._decisions.values():
            if decision.archived or (not final and decision.status not in ("accepted", "rejected", "failed")):
                continue
            snapshot = decision.snapshot()
            self._completed.append(snapshot)
            self._previous = snapshot
            decision.archived = True

    def snapshot(self) -> CommentarySnapshot:
        current = self._decisions.get(self._focus) if self._focus is not None else None
        previous = self._previous
        if (
            current is not None
            and previous is not None
            and previous.player_id == current.player_id
            and previous.number == current.number
        ):
            previous = None
        return CommentarySnapshot(current.snapshot() if current is not None else None, previous)

    def drain_completed(self, *, final: bool = False) -> tuple[DecisionCommentary, ...]:
        if final:
            self._archive_finished(final=True)
        completed = tuple(self._completed)
        self._completed.clear()
        return completed


def _header(decision: DecisionCommentary) -> Text:
    header = Text(decision.name, style="bold cyan")
    parts = []
    if decision.hand != "" and decision.play != "":
        parts.append(f"Hand {decision.hand} / Play {decision.play}")
    if decision.number != "":
        parts.append(f"Decision {decision.number}")
    if decision.attempt != "":
        parts.append(f"Attempt {decision.attempt}")
    if len(parts) > 0:
        header.append(" · " + " · ".join(parts), style="dim")
    status_style = "yellow" if decision.status in ("deciding", "retrying") else "green"
    if decision.status in ("failed", "rejected"):
        status_style = "red"
    header.append(f" · {decision.status}", style=status_style)
    settings = []
    if decision.model != "":
        settings.append(decision.model)
    if decision.effort != "":
        settings.append(f"requested effort: {decision.effort}")
    if decision.phase == "awaiting_row_choice":
        settings.append("choose a row")
    if len(settings) > 0:
        header.append("\n" + " · ".join(settings), style="dim")
    return header


def _proposal_text(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return False
    try:
        value = json.loads(stripped)
    except (ValueError, RecursionError):
        return True
    return isinstance(value, dict) and "actions" in value and "protocol_version" in value


def _message_body(message: Message) -> RenderableType:
    if _proposal_text(message.text):
        return Text("Structured output received" if message.complete else "Receiving structured output…", style="dim")
    body = message.text
    label = {
        "model_text": "Commentary",
        "reasoning_summary": "Reasoning supplied by client",
        "stderr": "Process diagnostic",
    }.get(message.kind, message.kind)
    suffix = "" if message.complete else " · streaming"
    if message.kind == "stderr":
        rendered: RenderableType = Text(body)
    else:
        rendered = Markdown(body, hyperlinks=False)
    footer = Text("Text truncated; full output is in the model trace.", style="dim") if message.truncated else Text("")
    return Group(Text(label + suffix, style="bold"), rendered, footer)


def _tool_summary(tools: tuple[Tool, ...]) -> Text:
    completed = sum(tool.status in ("completed", "finished") for tool in tools)
    active = [tool for tool in tools if tool.status not in ("completed", "finished")]
    text = Text("Tools: ", style="dim")
    descriptions = [f"{tool.name}: {tool.status}" for tool in active[-2:]]
    if completed > 0:
        descriptions.append(f"{completed} completed")
    text.append(" · ".join(descriptions))
    return text


def _candidate_table(candidates: Candidates) -> Group:
    detail = candidates.objective + " · lower is better"
    if candidates.horizon != "":
        detail += f" · {candidates.horizon} plays"
    if candidates.samples is not None:
        detail += f" · {candidates.samples} samples"
    table = Table.grid(padding=(0, 2))
    for _ in range(4):
        table.add_column()
    table.add_row(Text("Card", style="dim"), Text("Value", style="dim"), Text("Above best", style="dim"), Text(""))
    selected = list(candidates.values[:3])
    chosen = next((entry for entry in candidates.values if entry[0] == candidates.chosen), None)
    if chosen is not None and chosen not in selected:
        selected.append(chosen)
    best = candidates.values[0][1] if len(candidates.values) > 0 else 0.0
    for card, value in selected:
        style = "bold yellow" if card == candidates.chosen else ""
        value_text = f"{value:.1%}" if candidates.probability else f"{value:.2f}"
        gap = f"+{(value - best) * 100:.1f} pp" if candidates.probability else f"+{value - best:.2f}"
        table.add_row(
            Text(f"[{card}]", style=style),
            Text(value_text, style=style),
            Text(gap, style=style),
            Text("chosen" if card == candidates.chosen else "", style=style),
        )
    return Group(Text(detail, style="dim"), table)


def render_decision(decision: DecisionCommentary, *, history: bool = False) -> Group:
    contents: list[RenderableType] = [_header(decision)]
    if len(decision.choices) > 0:
        contents.append(Text(f"{decision.choice_status}: " + "; ".join(decision.choices), style="bold"))
    if decision.explanation != "":
        state = {"Accepted": "accepted move", "Rejected": "rejected proposal", "Failed": "failed proposal"}.get(
            decision.choice_status, "failed proposal" if decision.status == "failed" else "provisional"
        )
        label = f"Decision explanation · {state}"
        contents.extend((Text(label, style="bold"), Markdown(decision.explanation, hyperlinks=False)))
    current_messages = tuple(
        message for message in decision.messages if message.invocation in ("", decision.invocation)
    )
    messages = decision.messages if history else current_messages[-2:]
    grouped_invocations = {message.invocation for message in messages}
    previous_invocation: str | None = None
    for message in messages:
        if len(decision.choices) > 0 and _proposal_text(message.text):
            continue
        if history and len(grouped_invocations) > 1 and message.invocation != previous_invocation:
            contents.append(Text(f"Invocation {message.invocation}", style="bold dim"))
            previous_invocation = message.invocation
        contents.append(_message_body(message))
    if decision.candidates is not None:
        contents.append(_candidate_table(decision.candidates))
    if len(decision.tools) > 0:
        contents.append(_tool_summary(decision.tools))
    if decision.diagnostic != "":
        contents.append(Text(decision.diagnostic, style="red"))
    if len(contents) == 1:
        contents.append(Text("Waiting for commentary or a proposed move…", style="dim"))
    return Group(*contents)


@dataclass(frozen=True)
class _HeightLimited:
    contents: RenderableType
    lines: int

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        rendered = console.render_lines(self.contents, options, pad=False)
        truncated = len(rendered) > self.lines
        limit = max(0, self.lines - 1) if truncated else self.lines
        for line in rendered[:limit]:
            yield from line
            yield Segment.line()
        if truncated:
            yield Text(
                "… More in terminal scrollback after this decision", style="dim", no_wrap=True, overflow="ellipsis"
            )


def render_commentary(snapshot: CommentarySnapshot, *, width: int, height: int | None = None) -> Panel:
    if snapshot.current is None:
        contents: RenderableType = Text("Waiting for model commentary or strategy evaluations…", style="dim")
    else:
        entries: list[RenderableType] = [render_decision(snapshot.current)]
        if snapshot.previous is not None:
            previous = snapshot.previous
            summary = f"Previous: {previous.name} · {previous.status}"
            if len(previous.choices) > 0:
                summary += " · " + "; ".join(previous.choices)
            entries.append(Text(summary, style="dim"))
        contents = Group(*entries)
    if height is not None:
        contents = _HeightLimited(contents, max(1, height - 2))
    return Panel(
        contents, title="Operator commentary · may include private information", border_style="magenta", width=width
    )
