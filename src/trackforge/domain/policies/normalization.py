"""Value normalization used by policy selection and explainability.

Normalization is only an equality/ordering device.  Resolvers always keep and
return the original claim value; nothing in this module is allowed to rewrite
an observation or a claim.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, cast

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.identity import Transformation

# Canonical field names and accepted aliases from tags, older plans, and
# metadata formats.  Aliases are normalized before policy lookup.
FIELD_ALIASES: dict[str, str] = {
    "artists": "artist",
    "album_artist": "albumartist",
    "album_artists": "albumartist",
    "track_total": "tracktotal",
    "tracktotal": "tracktotal",
    "disc_total": "disctotal",
    "disctotal": "disctotal",
    "original_date": "originaldate",
    "cover": "artwork",
    "cover_art": "artwork",
    "mb_release_group_id": "mb_release_group_id",
    "musicbrainz_recording_id": "mb_recording_id",
    "musicbrainz_release_id": "mb_release_id",
    "musicbrainz_release_group_id": "mb_release_group_id",
    "musicbrainz_artist_id": "mb_artist_id",
    "musicbrainz_albumartist_id": "mb_albumartist_id",
}

# Canonical claim value fields that can feed an output field.
CLAIM_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "artist": ("artist", "artists"),
    "albumartist": ("albumartist", "album_artist"),
    "tracktotal": ("tracktotal", "track_total"),
    "disctotal": ("disctotal", "disc_total"),
    "originaldate": ("originaldate", "original_date"),
    "artwork": ("artwork", "cover", "cover_art"),
    "source": ("source", "source_url", "source_id"),
}

MULTI_VALUE_FIELDS = frozenset({"artist", "genre"})

ARTIST_ROLES = frozenset(
    {
        "main",
        "featured",
        "remixer",
        "composer",
        "lyricist",
        "producer",
        "albumartist",
    }
)

_WHITESPACE_RE = re.compile(r"\s+")
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'"})
_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‟": '"'})
_FEATURING_RE = re.compile(
    r"\s*\b(?:featuring|feat|ft)\b\.?\s*",
    re.IGNORECASE,
)
_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|&|\bvs\b\.?|\bx\b|×)\s*",
    re.IGNORECASE,
)
_TOPIC_SUFFIX_RE = re.compile(r"\s*[-–—]\s*topic\s*$", re.IGNORECASE)
_VEVO_RE = re.compile(r"vevo$", re.IGNORECASE)

_SOURCE_NOISE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("official_video", re.compile(r"\bofficial\s+(?:music\s+)?video\b", re.I)),
    ("official_audio", re.compile(r"\bofficial\s+audio\b", re.I)),
    ("lyric_video", re.compile(r"\blyric(?:s)?\s+video\b", re.I)),
    ("lyrics", re.compile(r"\blyrics\b", re.I)),
    ("music_video", re.compile(r"\bmusic\s+video\b", re.I)),
    ("visualizer", re.compile(r"\bvisuali[sz]er\b", re.I)),
    ("hd", re.compile(r"\b(?:hd|hq)\b", re.I)),
    ("4k", re.compile(r"\b(?:4k|8k|uhd)\b", re.I)),
    ("mv", re.compile(r"\bmv\b", re.I)),
    ("audio", re.compile(r"\baudio\b", re.I)),
    ("topic_channel", re.compile(r"\s*[-–—]\s*topic\s*$", re.I)),
    ("vevo", re.compile(r"vevo\s*$", re.I)),
)

_DATE_RE = re.compile(r"^(\d{4})(?:[-/.](\d{1,2}))?(?:[-/.](\d{1,2}))?$")
_KEY_RE = re.compile(
    r"^(?P<root>[A-Ga-g])\s*(?P<accidental>[#b♯♭]?)\s*"
    r"(?P<quality>m|min|minor|maj|major)?$",
    re.IGNORECASE,
)

_QUALIFIER_LABELS: dict[Transformation, str] = {
    Transformation.NIGHTCORE: "Nightcore",
    Transformation.SPED_UP: "Sped Up",
    Transformation.SLOWED: "Slowed",
    Transformation.REVERB: "Reverb",
    Transformation.EIGHT_D: "8D",
    Transformation.REMIX: "Remix",
    Transformation.BOOTLEG: "Bootleg",
    Transformation.MASHUP: "Mashup",
    Transformation.COVER: "Cover",
    Transformation.INSTRUMENTAL: "Instrumental",
    Transformation.ACAPELLA: "Acapella",
    Transformation.KARAOKE: "Karaoke",
    Transformation.RADIO_EDIT: "Radio Edit",
    Transformation.EXTENDED: "Extended",
    Transformation.LIVE: "Live",
    Transformation.ACOUSTIC: "Acoustic",
    Transformation.DEMO: "Demo",
}

_TRANSFORMATION_PATTERNS: tuple[tuple[Transformation, tuple[re.Pattern[str], ...]], ...] = (
    (Transformation.NIGHTCORE, (re.compile(r"\bnightcore\b", re.I),)),
    (
        Transformation.SPED_UP,
        (
            re.compile(r"\bsped[\s_-]*up\b", re.I),
            re.compile(r"\bspeed[\s_-]*up\b", re.I),
            re.compile(r"\bspeedup\b", re.I),
        ),
    ),
    (
        Transformation.SLOWED,
        (
            re.compile(r"\bslowed\b", re.I),
            re.compile(r"\bslow[\s_-]*down\b", re.I),
        ),
    ),
    (
        Transformation.REVERB,
        (
            re.compile(r"\breverb(?:ed)?\b", re.I),
            re.compile(r"\bslowed\s*\+\s*reverb\b", re.I),
        ),
    ),
    (
        Transformation.EIGHT_D,
        (re.compile(r"\b8[\s_-]?d\b", re.I), re.compile(r"\beight[\s_-]?d\b", re.I)),
    ),
    (
        Transformation.REMIX,
        (
            re.compile(r"\bremix(?:ed)?\b", re.I),
            re.compile(r"\bremix\b", re.I),
        ),
    ),
    (Transformation.BOOTLEG, (re.compile(r"\bbootleg\b", re.I),)),
    (Transformation.MASHUP, (re.compile(r"\bmash[\s_-]?up\b", re.I),)),
    (
        Transformation.COVER,
        (
            re.compile(r"\bcover(?:ed)?\b", re.I),
            re.compile(r"\bcover\s+version\b", re.I),
        ),
    ),
    (
        Transformation.INSTRUMENTAL,
        (
            re.compile(r"\binstrumental\b", re.I),
            re.compile(r"\binst\.?\b", re.I),
        ),
    ),
    (
        Transformation.ACAPELLA,
        (
            re.compile(r"\ba[\s_-]?capp?ella\b", re.I),
            re.compile(r"\bacapella\b", re.I),
        ),
    ),
    (Transformation.KARAOKE, (re.compile(r"\bkaraoke\b", re.I),)),
    (
        Transformation.RADIO_EDIT,
        (re.compile(r"\bradio[\s_-]?edit\b", re.I),),
    ),
    (Transformation.EXTENDED, (re.compile(r"\bextended\b", re.I),)),
    (Transformation.LIVE, (re.compile(r"\blive\b", re.I),)),
    (Transformation.ACOUSTIC, (re.compile(r"\bacoustic\b", re.I),)),
    (Transformation.DEMO, (re.compile(r"\bdemo\b", re.I),)),
)

_TRANSFORMATION_RANK = {
    transformation: index for index, (transformation, _) in enumerate(_TRANSFORMATION_PATTERNS)
}


class DatePrecision(StrEnum):
    """How much of a date is actually known."""

    YEAR = "year"
    MONTH = "month"
    DAY = "day"


@dataclass(frozen=True, slots=True)
class ArtistCredit:
    """One artist extracted from a credit string."""

    name: str
    role: str = "main"
    join_phrase: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise DomainValidationError("artist credit name must be a non-empty string")
        if self.role not in ARTIST_ROLES:
            raise DomainValidationError(
                "artist credit role is not supported",
                context={"role": self.role},
            )


@dataclass(frozen=True, slots=True)
class ParsedDate:
    """A date normalized without inventing precision."""

    value: str
    year: int
    month: int | None = None
    day: int | None = None

    @property
    def precision(self) -> DatePrecision:
        if self.day is not None:
            return DatePrecision.DAY
        if self.month is not None:
            return DatePrecision.MONTH
        return DatePrecision.YEAR

    def as_date(self) -> date | None:
        if self.month is None or self.day is None:
            return None
        return date(self.year, self.month, self.day)


def canonical_field(field: str) -> str:
    """Return the canonical field name for an alias."""

    if not isinstance(field, str) or not field.strip():
        raise DomainValidationError("field must be a non-empty string")
    normalized = field.strip().lower()
    return FIELD_ALIASES.get(normalized, normalized)


def claim_fields_for(field: str) -> tuple[str, ...]:
    """Return all claim field names that may feed ``field``."""

    canonical = canonical_field(field)
    return CLAIM_FIELD_ALIASES.get(canonical, (canonical,))


def _require_text(value: object) -> str:
    if not isinstance(value, str):
        raise DomainValidationError(
            "value must be a string",
            context={"actual_type": type(value).__name__},
        )
    return value


def normalize_text(value: str) -> str:
    """Apply Unicode NFKC, trim, and collapse whitespace.

    Case is deliberately preserved; use :func:`comparison_key` when a
    case-insensitive comparison is required.
    """

    text = _require_text(value)
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_APOSTROPHES).translate(_QUOTES)
    text = text.replace("\u00a0", " ")
    return _WHITESPACE_RE.sub(" ", text).strip()


# Common spelling used by callers that think of "normalizing a string".
normalize_string = normalize_text


def comparison_key(value: str) -> str:
    """Return a casefolded, punctuation-insensitive comparison key.

    Bracket/qualifier content is preserved so ``Song`` and
    ``Song (Remastered)`` do not collapse into one value.
    """

    text = normalize_text(value).casefold()
    text = _FEATURING_RE.sub(" feat ", text)
    text = re.sub(r"\band\b", " and ", text)
    text = re.sub(r"\bvs\b\.?", " versus ", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s()\[\]【】]", "", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def normalize_whitespace(value: str) -> str:
    """Alias for :func:`normalize_text` kept for discoverability."""

    return normalize_text(value)


# Names used by callers that treat comparison normalization separately.
normalize_for_comparison = comparison_key
normalize_comparison = comparison_key


def strip_topic_suffix(value: str) -> str:
    """Turn YouTube's ``Artist - Topic`` channel into ``Artist``."""

    text = normalize_text(value)
    return _TOPIC_SUFFIX_RE.sub("", text).strip()


def is_vevo(value: str) -> bool:
    """Return whether a channel name is VEVO rather than an artist name."""

    text = strip_topic_suffix(value)
    return bool(_VEVO_RE.search(text)) or comparison_key(text) == "vevo"


def normalize_artist_name(value: str) -> str:
    """Normalize one artist name and drop non-artist channel suffixes.

    ``VEVO`` and ``Artist - Topic`` carry source provenance, not an artist.
    VEVO returns an empty string so callers can keep it only in ``source``.
    """

    text = strip_topic_suffix(value)
    if not text or is_vevo(text):
        return ""
    return text


# Common alias used by callers.
normalize_artist = normalize_artist_name


def parse_artists(
    value: str,
    *,
    default_role: str = "main",
) -> tuple[ArtistCredit, ...]:
    """Split a free-form credit into artist credits using heuristic roles.

    The split is intentionally conservative: commas, ``&``, ``vs.``, and
    standalone ``x``/``×`` separate artists; ``feat.``/``ft.``/``featuring``
    marks every following artist as featured.  Normalization never decides
    that a credit is authoritative; it only produces comparison candidates.
    """

    text = normalize_text(value)
    if not text:
        return ()
    if default_role not in ARTIST_ROLES:
        raise DomainValidationError(
            "artist role is not supported",
            context={"role": default_role},
        )

    parts = _FEATURING_RE.split(text)
    credits: list[ArtistCredit] = []
    for index, part in enumerate(parts):
        role = default_role if index == 0 else "featured"
        for segment in _ARTIST_SPLIT_RE.split(part):
            name = normalize_artist_name(segment)
            if not name:
                continue
            credits.append(ArtistCredit(name=name, role=role))
    return tuple(credits)


# Common alias used by callers.
split_artists = parse_artists


def normalize_artists(values: object) -> tuple[ArtistCredit, ...]:
    """Parse an iterable of artist strings into ordered credits."""

    if isinstance(values, (str, bytes, bytearray)):
        return parse_artists(str(values))
    try:
        items: tuple[object, ...] = tuple(values)  # type: ignore[arg-type]
    except TypeError:
        raise DomainValidationError("artist values must be an iterable of strings") from None
    result: list[ArtistCredit] = []
    for item in items:
        if not isinstance(item, str):
            raise DomainValidationError("artist values must be strings")
        result.extend(parse_artists(item))
    return tuple(result)


def detect_source_noise(value: str) -> tuple[str, ...]:
    """Return markers for YouTube-style source noise in ``value``.

    Markers are metadata; the original string is never stripped here.
    """

    text = normalize_text(value)
    found: list[str] = []
    for marker, pattern in _SOURCE_NOISE_PATTERNS:
        if pattern.search(text) and marker not in found:
            found.append(marker)
    return tuple(found)


# Alias used by some callers.
source_noise_markers = detect_source_noise


def has_source_noise(value: str) -> bool:
    """Return whether ``value`` contains any known source-noise marker."""

    return bool(detect_source_noise(value))


def parse_date(value: str) -> ParsedDate | None:
    """Parse an ISO-like date without guessing missing components.

    Only ``YYYY``, ``YYYY-MM``, and ``YYYY-MM-DD`` are accepted (slash and
    dot separators are tolerated).  A winter/month is never inferred from a
    more precise value, and an invalid calendar date is rejected.
    """

    text = normalize_text(value)
    match = _DATE_RE.fullmatch(text)
    if match is None:
        return None
    year = int(match.group(1))
    month_text = match.group(2)
    day_text = match.group(3)
    if year < 1 or year > 9999:
        return None
    month = int(month_text) if month_text is not None else None
    day = int(day_text) if day_text is not None else None
    if month is not None and not 1 <= month <= 12:
        return None
    if day is not None and month is None:
        return None
    if day is not None:
        try:
            date(year, month, day)  # type: ignore[arg-type]
        except ValueError:
            return None
    if month is None:
        rendered = f"{year:04d}"
    elif day is None:
        rendered = f"{year:04d}-{month:02d}"
    else:
        rendered = f"{year:04d}-{month:02d}-{day:02d}"
    return ParsedDate(value=rendered, year=year, month=month, day=day)


def normalize_date(value: str) -> str | None:
    """Return an ISO date with exactly the precision supplied, or ``None``."""

    parsed = parse_date(value)
    return None if parsed is None else parsed.value


def normalize_bpm(value: object) -> float | None:
    """Return a finite positive BPM without half/double-time correction."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = normalize_text(value).replace(",", ".")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def normalize_key(value: str) -> str | None:
    """Normalize a musical key to ``C``, ``C#``, ``Db``, ``Am``, ``F#m``, ...

    The original claim value remains the source of truth; this function only
    returns a comparison/format-normalized spelling.
    """

    text = normalize_text(value)
    text = text.replace("♯", "#").replace("♭", "b")
    match = _KEY_RE.fullmatch(text)
    if match is None:
        return None
    root = match.group("root").upper()
    accidental = match.group("accidental").replace("♯", "#").replace("♭", "b")
    quality = match.group("quality")
    suffix = ""
    if quality is not None:
        lowered = quality.lower()
        if lowered in {"m", "min", "minor"}:
            suffix = "m"
        elif lowered in {"maj", "major"}:
            suffix = ""
    return f"{root}{accidental}{suffix}"


def display_qualifier(transformation: Transformation | str) -> str:
    """Return the human-readable title qualifier for a transformation."""

    value = _coerce_transformation(transformation)
    return _QUALIFIER_LABELS.get(value, value.value.replace("_", " ").title())


def _coerce_transformation(value: Transformation | str) -> Transformation:
    if isinstance(value, Transformation):
        return value
    if isinstance(value, str):
        try:
            return Transformation(value)
        except ValueError as exc:
            raise DomainValidationError(
                "unknown transformation",
                context={"value": value},
            ) from exc
    raise DomainValidationError("transformation must be a Transformation or string")


def detect_transformations(value: str) -> tuple[Transformation, ...]:
    """Detect derivative markers in a title, comment, or tag string.

    Detection is evidence-only: a caller must not treat an empty result as
    proof that a track is *not* a derivative.
    """

    text = normalize_text(value)
    found: list[Transformation] = []
    for transformation, patterns in _TRANSFORMATION_PATTERNS:
        if any(pattern.search(text) for pattern in patterns) and transformation not in found:
            found.append(transformation)
    return tuple(found)


# Common alias used by callers.
detect_derivatives = detect_transformations


def rank_transformations(
    transformations: object,
) -> tuple[Transformation, ...]:
    """Return transformations ordered by the documented title priority."""

    items: tuple[object, ...]
    if isinstance(transformations, (str, Transformation)):
        items = (transformations,)
    else:
        try:
            items = tuple(transformations)  # type: ignore[arg-type]
        except TypeError:
            raise DomainValidationError(
                "transformations must be an iterable",
            ) from None
    values = {_coerce_transformation(cast("Transformation | str", item)) for item in items}
    return tuple(sorted(values, key=lambda item: _TRANSFORMATION_RANK.get(item, 999)))


def title_has_qualifier(title: str, transformation: Transformation | str) -> bool:
    """Return whether a title already carries the qualifier for a transformation."""

    text = normalize_text(title).casefold()
    candidate = _coerce_transformation(transformation)
    label = display_qualifier(candidate).casefold()
    value = candidate.value.replace("_", " ").casefold()
    return label in text or value in text


def apply_qualifier(title: str, transformation: Transformation | str) -> str:
    """Append `` (Qualifier)`` unless the qualifier is already present."""

    base = normalize_text(title)
    if not base:
        raise DomainValidationError("cannot add a qualifier to an empty title")
    candidate = _coerce_transformation(transformation)
    if title_has_qualifier(base, candidate):
        return base
    return f"{base} ({display_qualifier(candidate)})"


def ensure_qualifier(title: str, transformation: Transformation | str) -> str:
    """Alias for :func:`apply_qualifier`."""

    return apply_qualifier(title, transformation)


def derive_title(
    title: str,
    transformations: object,
) -> str:
    """Derive a derivative title from a base title and transformations."""

    base = normalize_text(title)
    ranked = rank_transformations(transformations)
    if not ranked or not base:
        return base
    return apply_qualifier(base, ranked[0])


def value_equal(field: str, left: object, right: object) -> bool:
    """Compare two field values using policy normalization.

    The original values are untouched; this helper is only used for grouping
    and alternative-rejection explanations.
    """

    return bool(_normalized(field, left) == _normalized(field, right))


def _normalized(field: str, value: object) -> Any:
    canonical = canonical_field(field)
    if value is None:
        return None
    if canonical in {"date", "originaldate"} and isinstance(value, str):
        return normalize_date(value) or comparison_key(value)
    if canonical == "bpm":
        return normalize_bpm(value)
    if canonical == "key" and isinstance(value, str):
        return normalize_key(value) or comparison_key(value)
    if canonical in {"artist", "albumartist"} and isinstance(value, str):
        return comparison_key(normalize_artist_name(value))
    if isinstance(value, str):
        return comparison_key(value)
    if isinstance(value, (list, tuple)):
        return tuple(comparison_key(item) if isinstance(item, str) else item for item in value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


split_artist_credit = parse_artists
format_qualifier = display_qualifier


__all__ = [
    "ARTIST_ROLES",
    "CLAIM_FIELD_ALIASES",
    "FIELD_ALIASES",
    "MULTI_VALUE_FIELDS",
    "ArtistCredit",
    "DatePrecision",
    "ParsedDate",
    "apply_qualifier",
    "canonical_field",
    "claim_fields_for",
    "comparison_key",
    "derive_title",
    "detect_derivatives",
    "detect_source_noise",
    "detect_transformations",
    "display_qualifier",
    "ensure_qualifier",
    "has_source_noise",
    "is_vevo",
    "normalize_artist",
    "normalize_artist_name",
    "normalize_artists",
    "normalize_bpm",
    "normalize_date",
    "normalize_comparison",
    "normalize_for_comparison",
    "normalize_key",
    "normalize_string",
    "normalize_text",
    "normalize_whitespace",
    "parse_artists",
    "parse_date",
    "rank_transformations",
    "source_noise_markers",
    "split_artist_credit",
    "split_artists",
    "strip_topic_suffix",
    "title_has_qualifier",
    "value_equal",
]
