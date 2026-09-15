"""Private native-client workspaces for one harness bot session."""

import json
import math
import shlex
import sys
from pathlib import Path

from sixnimmt.arena.bots.external_harnesses.broker import SeatSession


def _private_write(path: Path, contents: str, *, executable: bool = False) -> None:
    with path.open("x", encoding="utf-8") as handle:
        path.chmod(0o700 if executable else 0o600)
        handle.write(contents)


def write_connection_bundle(session: SeatSession, directory: Path, endpoint: str, native_client: str) -> Path:
    """Create a private workspace without exposing a credential in instructions."""
    workspace = directory / "seats" / session.player_id
    workspace.mkdir(mode=0o700, parents=True, exist_ok=False)
    info = session.get_game_info(session.session_id)
    prompt = f"Read PLAY.md and play the complete match for session_id {session.session_id}."
    instructions = (
        f"# Play 6 nimmt!\n\nYour seat session_id is `{session.session_id}`.\n\n"
        "Call `get_game_info(session_id)`, then `play(session_id)` without a proposal to enter the table. "
        "Keep playing until a terminal result arrives. When a decision arrives, inspect its offer and "
        "choose a proposal with protocol_version, session_id, decision_id, submission_id, view_id, actions "
        "and memory. Copy the supplied IDs; use a new unique submission_id for each new proposal. "
        "Call `play(session_id, proposal)` once. The move is submitted immediately; the call waits for "
        "the next decision, rejection or terminal result. Follow the returned offer.\n\n"
        "A timeout can happen after a move was accepted. Recover with `play(session_id)` without a "
        "proposal, or retry the exact saved proposal with the same submission_id. Never submit an "
        "old move using a new ID because its reply was lost.\n\n"
        "Use only your offered view as game information. Other seat workspaces and controller traces "
        "contain private information and are outside your game tools. This is a trusted local game. "
        "You may keep your own notes and analysis in this workspace.\n\n"
        f"## Game instructions\n\n{info['instructions']}\n"
    )
    _private_write(workspace / "PLAY.md", instructions)
    _private_write(workspace / "AGENTS.md", "Read PLAY.md for this seat's game instructions.\n")
    _private_write(workspace / "CLAUDE.md", "Read PLAY.md for this seat's game instructions.\n")
    _private_write(workspace / "prompt.txt", prompt + "\n")
    _private_write(workspace / "game-info.json", json.dumps(info, indent=2) + "\n")
    environment = {"SIXNIMMT_ENDPOINT": endpoint, "SIXNIMMT_CREDENTIAL": session.credential}
    bridge = {
        "command": sys.executable,
        "args": ["-m", "sixnimmt.arena.bots.external_harnesses.mcp"],
        "env": environment,
    }
    client_timeout = math.ceil(session.wait_timeout_seconds + 60)
    claude_bridge = {**bridge, "type": "stdio", "timeout": client_timeout * 1000}
    _private_write(
        workspace / "claude-mcp.json", json.dumps({"mcpServers": {"sixnimmt": claude_bridge}}, indent=2) + "\n"
    )
    codex_environment = {"CODEX_MCP_PROTOCOL_VERSION": "2026-07-28"}
    codex_entry = (
        "mcp_servers={sixnimmt={command="
        + json.dumps(sys.executable)
        + ",args="
        + json.dumps(bridge["args"])
        + ",tool_timeout_sec="
        + str(client_timeout)
        + ",env_vars="
        + json.dumps(list(environment))
        + ",env={"
        + ",".join(key + "=" + json.dumps(value) for key, value in codex_environment.items())
        + "}}}"
    )
    _private_write(workspace / "codex-settings.toml", "features.mcp_2026_07_28=true\n" + codex_entry + "\n")
    if native_client == "claude":
        command = (
            "env MCP_SDK_GENERATION=v2 MCP_PROTOCOL_NEGOTIATION=auto CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=0 "
            + shlex.join(["claude", "--strict-mcp-config", "--mcp-config", str(workspace / "claude-mcp.json"), prompt])
        )
    else:
        command = shlex.join(["codex", "--enable", "mcp_2026_07_28", "-c", codex_entry, prompt])
    _private_write(
        workspace / "launch.sh",
        "#!/bin/sh\nset -eu\ncd "
        + shlex.quote(str(workspace))
        + "\n"
        + "\n".join("export " + key + "=" + shlex.quote(value) for key, value in environment.items())
        + "\nexec "
        + command
        + "\n",
        executable=True,
    )
    return workspace
