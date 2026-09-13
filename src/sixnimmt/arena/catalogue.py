"""Stable, resolved strategy identities for reproducible arena plans."""

import hashlib
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_serializer, field_validator, model_validator

from sixnimmt.arena.players import PlayerConfig, ResolvedPlayer, resolve_players


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def stable_id(prefix: str, value: Any) -> str:
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
    deterministic: bool

    @field_validator("options_json", "metadata_json", mode="before")
    @classmethod
    def freeze_objects(cls, value: object) -> str:
        return _object_json(value)

    @field_serializer("options_json", "metadata_json")
    def serialize_objects(self, value: str) -> dict[str, JsonValue]:
        return json.loads(value)

    @property
    def options(self) -> dict[str, JsonValue]:
        return json.loads(self.options_json)

    @property
    def metadata(self) -> dict[str, JsonValue]:
        return json.loads(self.metadata_json)

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
    try:
        factory_path = inspect.getsourcefile(factory)
    except TypeError:
        factory_path = None
    return {
        "module": getattr(factory, "__module__", type(factory).__module__),
        "name": getattr(factory, "__qualname__", type(factory).__qualname__),
        "source": None if factory_path is None else hashlib.sha256(Path(factory_path).read_bytes()).hexdigest(),
    }


def resolve_catalogue(candidates: tuple[CandidateConfig, ...]) -> tuple[CatalogueEntry, ...]:
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
        resolved = resolve_players([PlayerConfig(bot=candidate.bot, options=candidate.options)])[0]
        implementation_id = stable_id(
            "implementation",
            {
                "package": source_identity,
                "factory": _factory_identity(resolved.spec.build),
                "metadata": resolved.spec.metadata,
            },
        )
        config_id = stable_id(
            "config", {"bot": candidate.bot, "options": resolved.recorded_options, "implementation": implementation_id}
        )
        if config_id in identities:
            msg = f"duplicate resolved configuration: {key}; use repeated seats instead of catalogue aliases"
            raise ValueError(msg)
        keys.add(key)
        identities.add(config_id)
        entries.append(
            CatalogueEntry(
                config_id=config_id,
                key=key,
                label=key if candidate.label is None else candidate.label,
                family=candidate.family,
                bot=candidate.bot,
                options_json=canonical_json(resolved.recorded_options),
                metadata_json=canonical_json(resolved.metadata),
                implementation_id=implementation_id,
                deterministic=resolved.deterministic,
            )
        )
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


def resolve_frozen_catalogue(catalogue: tuple[CatalogueEntry, ...]) -> tuple[ResolvedPlayer, ...]:
    """Reject construction drift before executing configurations from a saved plan."""
    candidates = tuple(
        CandidateConfig(bot=entry.bot, key=entry.key, label=entry.label, family=entry.family, options=entry.options)
        for entry in catalogue
    )
    current = resolve_catalogue(candidates)
    for recorded, actual in zip(catalogue, current, strict=True):
        if recorded.config_id != actual.config_id or recorded != actual:
            msg = f"catalogue configuration {recorded.key!r} differs from the current implementation; build a new plan"
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
