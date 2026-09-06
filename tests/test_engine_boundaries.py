"""Engine boundaries: purity, layering, and rejection behavior."""

import ast
import pathlib

import pytest
from pydantic import TypeAdapter

from sixnimmt_server.engine.actions import Action
from sixnimmt_server.engine.errors import EngineRejection
from sixnimmt_server.engine.rules import GameRules, MatchProtocol
from sixnimmt_server.engine.setup import create_match
from sixnimmt_server.engine.state import Phase
from sixnimmt_server.engine.transition import transition

_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


def test_engine_never_imports_server_or_web_frameworks() -> None:
    engine_dir = pathlib.Path(__file__).resolve().parent.parent / "src" / "sixnimmt_server" / "engine"
    sources = " ".join(path.read_text() for path in engine_dir.glob("*.py"))

    assert "from server" not in sources
    assert "import server" not in sources
    assert "fastapi" not in sources.lower()


def test_failed_action_leaves_state_byte_identical() -> None:
    state, _ = create_match("m_01", ["a", "b"], match_seed=12345)
    frozen = state.model_dump_json()

    with pytest.raises(EngineRejection):
        transition(
            state,
            "a",
            _ACTION_ADAPTER.validate_python({"type": "select_card", "card": 104}),
            MatchProtocol(),
            GameRules(),
        )

    assert state.model_dump_json() == frozen


def test_rejection_after_some_commits_keeps_earlier_commits() -> None:
    state, _ = create_match("m_01", ["a", "b"], match_seed=12345)
    card = state.players[0].hand[0]
    select = _ACTION_ADAPTER.validate_python({"type": "select_card", "card": card})
    committed, _ = transition(state, "a", select, MatchProtocol(), GameRules())
    frozen = committed.model_dump_json()

    with pytest.raises(EngineRejection):
        transition(committed, "b", select, MatchProtocol(), GameRules())

    assert committed.model_dump_json() == frozen
    alice = next(player for player in committed.players if player.player_id == "a")
    assert alice.selection == card


def test_finished_match_rejects_every_action() -> None:
    state, _ = create_match("m_01", ["a", "b"], match_seed=12345)
    finished = state.model_copy(update={"phase": Phase.FINISHED})
    card = state.players[0].hand[0]

    with pytest.raises(EngineRejection):
        transition(
            finished,
            "a",
            _ACTION_ADAPTER.validate_python({"type": "select_card", "card": card}),
            MatchProtocol(),
            GameRules(),
        )


@pytest.mark.parametrize(
    "package, allowed",
    [
        ("common", {"common"}),
        ("engine", {"common", "engine"}),
        ("persistence", {"common", "engine", "persistence"}),
        ("arena", {"common", "engine", "persistence", "arena"}),
        ("server", {"common", "engine", "persistence", "server"}),
    ],
)
def test_packages_respect_dependency_direction(package: str, allowed: set[str]) -> None:
    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "sixnimmt_server"
    for path in (root / package).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = [node.module]
            for module in modules:
                if module.startswith("sixnimmt_server."):
                    assert module.split(".")[1] in allowed, (path, module)
                if package != "server":
                    assert module.split(".")[0] not in {"fastapi", "starlette", "uvicorn"}, (path, module)
