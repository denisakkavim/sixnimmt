"""Bearer tokens minted at match creation, and the viewer each one stands for.

A token carries its role; a caller never asks for one. That is what stops a
player from requesting the omniscient view of their own match (§5.4), and it
keeps the audience filter the only thing deciding what anyone sees.
"""

import secrets
from dataclasses import dataclass

from sixnimmt_server.engine.audience import Viewer
from sixnimmt_server.engine.views import ViewRole

_TOKEN_BYTES = 32


def mint_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


@dataclass(frozen=True)
class MatchTokens:
    """Everything minted for one match, returned to the creator exactly once."""

    player_tokens: dict[str, str]
    public_spectator_token: str
    omniscient_token: str


@dataclass(frozen=True)
class _Grant:
    match_id: str
    viewer: Viewer


class TokenRegistry:
    """Maps a bearer token to the viewer it authorises, for one match.

    The admin token is server-wide because `POST /matches` needs authority
    before any match exists.
    """

    def __init__(self, admin_token: str) -> None:
        self._admin_token = admin_token
        self._grants: dict[str, _Grant] = {}

    @property
    def admin_token(self) -> str:
        return self._admin_token

    def is_admin(self, token: str | None) -> bool:
        if token is None:
            return False
        return secrets.compare_digest(token, self._admin_token)

    def mint_for_match(self, match_id: str, player_ids: list[str]) -> MatchTokens:
        player_tokens = {player_id: mint_token() for player_id in player_ids}
        tokens = MatchTokens(
            player_tokens=player_tokens,
            public_spectator_token=mint_token(),
            omniscient_token=mint_token(),
        )
        for player_id, token in player_tokens.items():
            self._grants[token] = _Grant(match_id, Viewer(role=ViewRole.PLAYER, player_id=player_id))
        self._grants[tokens.public_spectator_token] = _Grant(match_id, Viewer(role=ViewRole.PUBLIC_SPECTATOR))
        self._grants[tokens.omniscient_token] = _Grant(match_id, Viewer(role=ViewRole.OMNISCIENT_OBSERVER))
        return tokens

    def viewer_for(self, token: str | None, match_id: str) -> Viewer | None:
        """The viewer this token authorises for this match, or None.

        None covers an unknown token and a token belonging to another match
        alike; the caller reports both as MATCH_NOT_FOUND.
        """
        if token is None:
            return None
        if self.is_admin(token):
            return Viewer(role=ViewRole.ADMIN)
        grant = self._grants.get(token)
        if grant is None or grant.match_id != match_id:
            return None
        return grant.viewer

    def release_match(self, match_id: str) -> None:
        """Forget an abandoned match's tokens once its record is gone."""
        for token in [token for token, grant in self._grants.items() if grant.match_id == match_id]:
            del self._grants[token]
