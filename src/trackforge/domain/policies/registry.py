"""Versioned default field policies and policy selection."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.decisions import (
    FieldPolicy,
    OnMissing,
    PolicyScope,
    PolicyStrategy,
)
from trackforge.domain.policies.normalization import canonical_field

# The v1 priority order is intentionally source-shaped: explicit user data and
# an authority provider outrank existing tags, while path-derived guesses stay
# a low-confidence fallback rather than being deleted.
_TITLE_PRIORITY = (
    "user_lock",
    "user",
    "musicbrainz",
    "item.title",
    "path.title",
    "llm",
    "unknown",
)
_ARTIST_PRIORITY = (
    "user_lock",
    "user",
    "musicbrainz",
    "item.artist",
    "path.artist",
    "llm",
    "unknown",
)
_SIMPLE_PRIORITY = ("user_lock", "user", "musicbrainz", "item.{field}", "llm", "unknown")
_COMMENT_PRIORITY = (
    "user_lock",
    "item.comment",
    "source_url",
    "source_id",
    "unknown",
)
_SOURCE_PRIORITY = (
    "user_lock",
    "item.source",
    "source_url",
    "source_id",
    "path.source",
    "unknown",
)
_ID_PRIORITY = ("user_lock", "external", "unknown")


def _simple(field: str, *, validators: tuple[str, ...] = ()) -> FieldPolicy:
    return FieldPolicy(
        id=field,
        field=field,
        scope=PolicyScope.GLOBAL,
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=tuple(token.format(field=field) for token in _SIMPLE_PRIORITY),
        validators=validators,
        on_missing=OnMissing.UNKNOWN,
        version=1,
    )


DEFAULT_POLICIES: tuple[FieldPolicy, ...] = (
    FieldPolicy(
        id="title",
        field="title",
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=_TITLE_PRIORITY,
        validators=("title",),
        version=1,
    ),
    FieldPolicy(
        id="title.derivative",
        field="title",
        conditions=({"derivative": True},),
        strategy=PolicyStrategy.DERIVE,
        priority=(
            "user_lock",
            "user",
            "musicbrainz",
            "derivative_credits",
            "item.title",
            "path.title",
            "llm",
            "unknown",
        ),
        validators=("title",),
        version=1,
    ),
    FieldPolicy(
        id="artist",
        field="artist",
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=_ARTIST_PRIORITY,
        validators=("artist",),
        version=1,
    ),
    FieldPolicy(
        id="artist.derivative",
        field="artist",
        conditions=({"derivative": True},),
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=(
            "user_lock",
            "user",
            "derivative_credits",
            "musicbrainz",
            "item.artist",
            "llm",
            "unknown",
        ),
        validators=("artist",),
        version=1,
    ),
    _simple("album", validators=("album",)),
    _simple("albumartist", validators=("albumartist",)),
    FieldPolicy(
        id="date",
        field="date",
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=("user_lock", "user", "musicbrainz", "item.date", "llm", "unknown"),
        validators=("date",),
        version=1,
    ),
    FieldPolicy(
        id="originaldate",
        field="originaldate",
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=("user_lock", "item.originaldate", "unknown"),
        validators=("originaldate",),
        version=1,
    ),
    FieldPolicy(
        id="genre",
        field="genre",
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=("user_lock", "user", "musicbrainz", "llm", "item.genre", "unknown"),
        validators=("genre",),
        version=1,
    ),
    _simple("track", validators=("track",)),
    _simple("tracktotal", validators=("tracktotal",)),
    _simple("disc", validators=("disc",)),
    _simple("disctotal", validators=("disctotal",)),
    FieldPolicy(
        id="comment",
        field="comment",
        strategy=PolicyStrategy.KEEP_EXISTING,
        priority=_COMMENT_PRIORITY,
        validators=("comment",),
        version=1,
    ),
    FieldPolicy(
        id="comment.derivative",
        field="comment",
        conditions=({"derivative": True},),
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=(
            "user_lock",
            "user",
            "derivative_credits",
            "item.comment",
            "llm",
            "unknown",
        ),
        validators=("comment",),
        version=1,
    ),
    _simple("remixer"),
    _simple("originalartist"),
    _simple("originaltitle"),
    _simple("qualifier"),
    FieldPolicy(
        id="source",
        field="source",
        strategy=PolicyStrategy.KEEP_EXISTING,
        priority=_SOURCE_PRIORITY,
        validators=("source",),
        version=1,
    ),
    _simple("bpm", validators=("bpm",)),
    _simple("key", validators=("key",)),
    FieldPolicy(
        id="cover",
        field="artwork",
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=(
            "user_lock",
            "cover_art_archive",
            "itunes",
            "deezer",
            "source_thumbnail",
            "item.artwork",
            "unknown",
        ),
        validators=("artwork",),
        version=1,
    ),
    FieldPolicy(
        id="lyrics",
        field="lyrics",
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=(
            "user_lock",
            "user",
            "lyrics_merge",
            "lrclib",
            "asr",
            "source_description",
            "item.lyrics",
            "unknown",
        ),
        validators=("lyrics",),
        version=1,
    ),
    FieldPolicy(
        id="synced_lyrics",
        field="synced_lyrics",
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=(
            "user_lock",
            "user",
            "lyrics_merge",
            "lrclib",
            "asr",
            "item.synced_lyrics",
            "unknown",
        ),
        validators=("lyrics",),
        version=1,
    ),
    FieldPolicy(
        id="elrc",
        field="elrc",
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=(
            "user_lock",
            "user",
            "lyrics_merge",
            "asr",
            "item.elrc",
            "unknown",
        ),
        validators=("lyrics",),
        version=1,
    ),
    _simple("label"),
    _simple("catalog_number"),
    _simple("barcode"),
    _simple("country"),
    FieldPolicy(
        id="compilation",
        field="compilation",
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=("user_lock", "item.compilation", "unknown"),
        validators=("compilation",),
        version=1,
    ),
    FieldPolicy(
        id="language",
        field="language",
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=(
            "user_lock",
            "user",
            "musicbrainz",
            "asr",
            "lyrics_merge",
            "item.language",
            "unknown",
        ),
        validators=("language",),
        version=1,
    ),
    *(
        FieldPolicy(
            id=field,
            field=field,
            strategy=PolicyStrategy.UNKNOWN,
            priority=_ID_PRIORITY,
            validators=(validator,),
            version=1,
        )
        for field, validator in (
            ("mb_recording_id", "mbid"),
            ("mb_release_id", "mbid"),
            ("mb_release_group_id", "mbid"),
            ("mb_artist_id", "mbid"),
            ("mb_albumartist_id", "mbid"),
            ("isrc", "isrc"),
            ("acoustid", "acoustid"),
        )
    ),
)


@dataclass(frozen=True, slots=True)
class PolicyRegistry:
    """An ordered, versioned collection of field policies."""

    policies: tuple[FieldPolicy, ...] = DEFAULT_POLICIES

    def __post_init__(self) -> None:
        object.__setattr__(self, "policies", tuple(self.policies))
        seen: set[tuple[str, str]] = set()
        for policy in self.policies:
            key = (policy.field, policy.id or policy.field)
            if key in seen:
                raise DomainValidationError(
                    "duplicate policy id for field",
                    context={"field": policy.field, "policy_id": policy.id},
                )
            seen.add(key)

    def __iter__(self) -> Iterator[FieldPolicy]:
        return iter(self.policies)

    def __len__(self) -> int:
        return len(self.policies)

    @property
    def fields(self) -> tuple[str, ...]:
        result: list[str] = []
        for policy in self.policies:
            if policy.field not in result:
                result.append(policy.field)
        return tuple(result)

    def for_field(
        self,
        field: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> FieldPolicy:
        """Return the most specific matching policy for ``field``."""

        canonical = canonical_field(field)
        values = dict(context or {})
        matching = [
            policy
            for policy in self.policies
            if policy.field == canonical and _conditions_match(policy.conditions, values)
        ]
        if not matching:
            raise DomainValidationError(
                "no policy registered for field",
                context={"field": canonical},
            )
        # Prefer the most specific conditional rule over the generic rule.
        matching.sort(key=lambda policy: len(policy.conditions), reverse=True)
        return matching[0]

    def get(self, field: str) -> FieldPolicy:
        """Return the base (unconditional) policy for ``field``."""

        canonical = canonical_field(field)
        for policy in self.policies:
            if policy.field == canonical and not policy.conditions:
                return policy
        raise DomainValidationError(
            "no policy registered for field",
            context={"field": canonical},
        )

    def get_policy(self, field: str) -> FieldPolicy:
        """Alias for :meth:`get`."""

        return self.get(field)

    def has_field(self, field: str) -> bool:
        canonical = canonical_field(field)
        return any(policy.field == canonical for policy in self.policies)

    def selected(self, context: Mapping[str, Any] | None = None) -> tuple[FieldPolicy, ...]:
        """Return one selected policy per field in registration order."""

        values = dict(context or {})
        result: list[FieldPolicy] = []
        for field in self.fields:
            matching = [
                policy
                for policy in self.policies
                if policy.field == field and _conditions_match(policy.conditions, values)
            ]
            if matching:
                matching.sort(key=lambda policy: len(policy.conditions), reverse=True)
                result.append(matching[0])
        return tuple(result)

    def versions(self, context: Mapping[str, Any] | None = None) -> tuple[str, ...]:
        return tuple(policy_version(policy) for policy in self.selected(context))

    def with_overrides(
        self,
        overrides: FieldPolicy | Sequence[FieldPolicy] | Mapping[str, FieldPolicy] | None,
    ) -> PolicyRegistry:
        """Return a registry where overrides replace every policy of a field."""

        if overrides is None:
            return self
        extra: tuple[FieldPolicy, ...]
        if isinstance(overrides, FieldPolicy):
            extra = (overrides,)
        elif isinstance(overrides, Mapping):
            extra = tuple(overrides.values())
        else:
            extra = tuple(overrides)
        if not extra:
            return self
        overridden_fields = {canonical_field(policy.field) for policy in extra}
        merged = (
            tuple(policy for policy in self.policies if policy.field not in overridden_fields)
            + extra
        )
        return PolicyRegistry(merged)


def _conditions_match(
    conditions: Iterable[object],
    context: Mapping[str, Any],
) -> bool:
    items = tuple(conditions)
    if not items:
        return True
    for condition in items:
        if isinstance(condition, str):
            key, separator, expected = condition.partition("=")
            key = key.strip()
            if not separator:
                if not context.get(key):
                    return False
                continue
            if str(context.get(key)) != expected.strip():
                return False
        elif isinstance(condition, Mapping):
            for key, expected in condition.items():
                if isinstance(expected, bool):
                    if bool(context.get(key)) is not expected:
                        return False
                elif context.get(key) != expected:
                    return False
        else:  # pragma: no cover - FieldPolicy validates conditions already
            return False
    return True


def policy_version(policy: FieldPolicy) -> str:
    """Return the stable ``id@version`` string stored in decision records."""

    identifier = policy.id or policy.field
    return f"{identifier}@{policy.version}"


def policy_version_number(policy: FieldPolicy) -> int:
    return policy.version


def default_registry() -> PolicyRegistry:
    """Return the v1 default policy registry."""

    return DEFAULT_POLICY_REGISTRY


DEFAULT_POLICY_REGISTRY = PolicyRegistry()
FieldPolicyRegistry = PolicyRegistry
DEFAULT_FIELD_POLICIES = DEFAULT_POLICIES
build_default_registry = default_registry


__all__ = [
    "DEFAULT_FIELD_POLICIES",
    "DEFAULT_POLICIES",
    "DEFAULT_POLICY_REGISTRY",
    "FieldPolicyRegistry",
    "PolicyRegistry",
    "build_default_registry",
    "default_registry",
    "policy_version",
    "policy_version_number",
]
