"""Automatic game-tool approval remains scoped to managed sixnimmt sessions."""

import tomllib
from pathlib import Path

from sixnimmt.arena.bots.external_harnesses.broker import SeatSession
from sixnimmt.arena.bots.external_harnesses.connection import write_connection_bundle
from sixnimmt.arena.bots.external_harnesses.drivers import CommandProfile, ManagedCommandDriver
from sixnimmt.arena.bots.external_harnesses.mcp import game_tools
from sixnimmt.engine.rules import GameRules, MatchProtocol


def test_native_codex_bundle_preserves_the_clients_game_tool_approval_policy(tmp_path: Path) -> None:
    session = SeatSession("a", "A", GameRules(), MatchProtocol())
    workspace = write_connection_bundle(session, tmp_path, "tcp://127.0.0.1:12345", "codex")
    settings = tomllib.loads((workspace / "codex-settings.toml").read_text())
    server = settings["mcp_servers"]["sixnimmt"]
    assert "default_tools_approval_mode" not in server
    assert "tools" not in server
    launch = (workspace / "launch.sh").read_text()
    assert "dangerously-bypass" not in launch
    assert "approval_policy" not in launch


def test_managed_codex_approves_only_the_two_game_tools(tmp_path: Path) -> None:
    driver = ManagedCommandDriver(CommandProfile(command=("codex",), kind="codex"), tmp_path)
    command = driver._match_command(tmp_path, 600)
    settings = next(argument for argument in command if argument.startswith("mcp_servers="))
    servers = tomllib.loads(settings)["mcp_servers"]
    assert set(servers) == {"sixnimmt"}
    assert servers["sixnimmt"]["tools"] == {
        "get_game_info": {"approval_mode": "approve"},
        "play": {"approval_mode": "approve"},
    }
    assert "default_tools_approval_mode" not in servers["sixnimmt"]
    assert not any("dangerously-bypass" in argument or "approval_policy" in argument for argument in command)


def test_game_tool_annotations_describe_state_changes_without_claiming_read_only() -> None:
    tools = {tool.name: tool for tool in game_tools()}
    info = tools["get_game_info"].annotations
    play = tools["play"].annotations
    assert info is not None
    assert play is not None
    assert info.read_only_hint is True
    assert info.destructive_hint is False
    assert play.read_only_hint is False
    assert play.destructive_hint is True
    assert info.open_world_hint is False
    assert play.open_world_hint is False
    assert info.idempotent_hint is True
    assert play.idempotent_hint is True
