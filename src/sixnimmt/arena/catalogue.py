"""Stable, resolved strategy identities for reproducible arena plans."""

import hashlib
import inspect
import json
from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)

from sixnimmt.arena.bots.base import TypedStrategyFactory
from sixnimmt.arena.players import PlayerConfig, ResolvedPlayer, resolve_players, resolve_strategy
from sixnimmt.arena.sessions import EXTERNAL_SEATS, resolve_external_seat

_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(
    dict[str, JsonValue], config=ConfigDict(allow_inf_nan=False)
)


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def stable_id(prefix: str, value: object) -> str:
    digest = hashlib.sha256(canonical_json(value).encode()).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _object_json(value: object) -> str:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        msg = "expected a JSON object"
        raise ValueError(msg)  # noqa: TRY004 - invalid JSON input should be a validation error
    return canonical_json(parsed)


class CandidateConfig(FrozenModel):
    """User-facing catalogue entry; keys refer to entries in lineup settings."""

    bot: str = Field(min_length=1)
    key: str | None = None
    label: str | None = None
    family: str = Field(default="unclassified", min_length=1)
    options: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("options")
    @classmethod
    def validate_options_json(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _JSON_OBJECT.validate_python(value)

    @model_validator(mode="after")
    def check_names(self) -> "CandidateConfig":
        if self.key == "" or self.label == "":
            msg = "candidate keys and labels must not be empty"
            raise ValueError(msg)
        return self


class CatalogueEntry(FrozenModel):
    """Resolved construction settings, with defensive copies of JSON objects."""

    config_id: str
    key: str
    label: str
    family: str
    bot: str
    options_json: str = Field(alias="options", repr=False)
    metadata_json: str = Field(alias="metadata", repr=False)
    implementation_id: str
    deterministic: bool = Field(strict=True)

    @field_validator("options_json", "metadata_json", mode="before")
    @classmethod
    def freeze_objects(cls, value: object) -> str:
        return _object_json(value)

    @field_serializer("options_json", "metadata_json")
    def serialize_objects(self, value: str) -> dict[str, JsonValue]:
        return _JSON_OBJECT.validate_json(value)

    @property
    def options(self) -> dict[str, JsonValue]:
        return _JSON_OBJECT.validate_json(self.options_json)

    @property
    def metadata(self) -> dict[str, JsonValue]:
        return _JSON_OBJECT.validate_json(self.metadata_json)

    def player_config(self) -> PlayerConfig:
        """Construction data contains no opponent labels or analysis metadata."""
        return PlayerConfig(bot=self.bot, options=self.options)


def _source_identity() -> str:
    # Include imported strategy helpers as well as the registered factory itself.
    package = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    paths = [*package.joinpath("arena", "bots").rglob("*.py"), *package.joinpath("engine").rglob("*.py")]
    paths.extend((package / "arena" / "players.py", package / "common" / "text.py"))
    for path in sorted(paths):
        digest.update(str(path.relative_to(package)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _factory_identity(factory: Callable[..., object]) -> dict[str, str | None]:
    if isinstance(factory, TypedStrategyFactory):
        factory = factory.constructor
    try:
        factory_path = inspect.getsourcefile(factory)
    except TypeError:
        factory_path = None
    return {
        "module": getattr(factory, "__module__", type(factory).__module__),
        "name": getattr(factory, "__qualname__", type(factory).__qualname__),
        "source": None if factory_path is None else hashlib.sha256(Path(factory_path).read_bytes()).hexdigest(),
    }


def _resolve_entry(candidate: CandidateConfig, source_identity: str, *, allow_external: bool) -> CatalogueEntry:
    key = candidate.bot if candidate.key is None else candidate.key
    if candidate.bot in EXTERNAL_SEATS:
        if not allow_external:
            msg = "external seats require an explicit fixed lineup"
            raise ValueError(msg)
        external = resolve_external_seat(candidate.bot, candidate.options)
        options = external.recorded_options
        fingerprint = stable_id("construction", {"bot": candidate.bot, "options": external.options})
        metadata = {
            **external.metadata,
            "construction_fingerprint": fingerprint,
            "private_options_required": external.options.get("command") is not None,
        }
        implementation_id = stable_id(
            "implementation",
            {
                "package": source_identity,
                "factory": _factory_identity(resolve_external_seat),
                "client": external.descriptor.client,
                "ownership": external.descriptor.ownership,
            },
        )
        config_id = stable_id(
            "config",
            {
                "bot": candidate.bot,
                "options": options,
                "construction_fingerprint": fingerprint,
                "implementation": implementation_id,
            },
        )
        deterministic = False
    else:
        resolved = resolve_strategy(candidate.bot, candidate.options)
        options = resolved.recorded_options
        metadata = resolved.metadata
        implementation_id = stable_id(
            "implementation",
            {
                "package": source_identity,
                "factory": _factory_identity(resolved.spec.build),
                "metadata": resolved.spec.metadata,
            },
        )
        config_id = stable_id(
            "config",
            {
                "bot": candidate.bot,
                "options": options,
                "implementation": implementation_id,
            },
        )
        deterministic = resolved.deterministic
    return CatalogueEntry(
        config_id=config_id,
        key=key,
        label=key if candidate.label is None else candidate.label,
        family=candidate.family,
        bot=candidate.bot,
        options_json=canonical_json(options),
        metadata_json=canonical_json(metadata),
        implementation_id=implementation_id,
        deterministic=deterministic,
    )


def resolve_catalogue(
    candidates: tuple[CandidateConfig, ...], *, allow_external: bool = False
) -> tuple[CatalogueEntry, ...]:
    if len(candidates) == 0:
        msg = "catalogue must contain at least one candidate"
        raise ValueError(msg)
    source_identity = _source_identity()
    entries: list[CatalogueEntry] = []
    keys: set[str] = set()
    identities: set[str] = set()
    for candidate in candidates:
        key = candidate.bot if candidate.key is None else candidate.key
        if key in keys:
            msg = f"duplicate catalogue key: {key}"
            raise ValueError(msg)
        try:
            entry = _resolve_entry(candidate, source_identity, allow_external=allow_external)
        except ValueError as error:
            msg = f"invalid options for catalogue entry {key!r} ({candidate.bot}): {error}"
            raise ValueError(msg) from error
        if entry.config_id in identities:
            msg = f"duplicate resolved configuration: {key}; use repeated seats instead of catalogue aliases"
            raise ValueError(msg)
        keys.add(key)
        identities.add(entry.config_id)
        entries.append(entry)
    return tuple(entries)


def reference_catalogue() -> tuple[CandidateConfig, ...]:
    """The versioned starting library of eleven inexpensive reference policies."""
    entries = [CandidateConfig(bot=name, family="card_order") for name in ("random", "lowest_card", "highest_card")]
    entries.extend(
        CandidateConfig(bot=name, family="board_and_hand")
        for name in ("lowest_fitting_card", "highest_fitting_card", "closest_gap", "coldest_row", "hand_flexibility")
    )
    entries.extend((
        CandidateConfig(
            bot="controlled_burn", family="captures_and_bait", options={"K": 5, "fallback_strategy": "closest_gap"}
        ),
        CandidateConfig(
            bot="count_threshold_bait",
            family="captures_and_bait",
            options={
                "intervening_card_threshold": 3,
                "candidate_ranking": "most_intervening",
                "fallback_strategy": "closest_gap",
            },
        ),
        CandidateConfig(
            bot="hand_aware_row_choice",
            family="row_choice",
            options={"max_extra_penalty": 2, "card_strategy": "closest_gap"},
        ),
    ))
    return tuple(entries)


def validate_frozen_catalogue(
    catalogue: tuple[CatalogueEntry, ...], players: Mapping[str, PlayerConfig] | None = None
) -> None:
    """Check construction drift without creating bots, workspaces, or sessions.

    Private command arguments are omitted from saved plans. A caller resuming
    such a definition must provide its original inputs, matched by config ID.
    """
    candidates: list[CandidateConfig] = []
    for entry in catalogue:
        player = None if players is None else players.get(entry.config_id)
        if player is not None and player.bot != entry.bot:
            msg = f"supplied player for {entry.key!r} differs from its frozen bot"
            raise ValueError(msg)
        if player is None and entry.metadata.get("private_options_required") is True:
            msg = f"catalogue entry {entry.key!r} requires original private construction options; provide players or build a new run"
            raise ValueError(msg)
        candidates.append(
            CandidateConfig(
                bot=entry.bot,
                key=entry.key,
                label=entry.label,
                family=entry.family,
                options=entry.options if player is None else player.options,
            )
        )
    current = resolve_catalogue(tuple(candidates), allow_external=True)
    for recorded, actual in zip(catalogue, current, strict=True):
        if recorded != actual:
            msg = f"catalogue configuration {recorded.key!r} differs from the current implementation or options; build a new plan"
            raise ValueError(msg)


def resolve_frozen_catalogue(catalogue: tuple[CatalogueEntry, ...]) -> tuple[ResolvedPlayer, ...]:
    """Compatibility construction path for registered strategies only."""
    validate_frozen_catalogue(catalogue)
    if any(entry.bot in EXTERNAL_SEATS for entry in catalogue):
        msg = "external catalogue entries require session-capable run execution"
        raise ValueError(msg)
    return tuple(resolve_players([entry.player_config() for entry in catalogue]))


REFERENCE_GROUP = (
    "lowest_fitting_card",
    "highest_fitting_card",
    "closest_gap",
    "coldest_row",
    "controlled_burn",
    "hand_aware_row_choice",
)
