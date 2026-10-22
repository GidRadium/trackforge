"""Domain policies: normalization, validation, strategies, and registry.

This package is pure domain code.  It must not import storage, SQLAlchemy,
HTTP, CLI, or framework modules.
"""

from __future__ import annotations

from trackforge.domain.policies import normalization as normalization
from trackforge.domain.policies import registry as registry
from trackforge.domain.policies import strategies as strategies
from trackforge.domain.policies import validators as validators
from trackforge.domain.policies.normalization import (
    ArtistCredit,
    ParsedDate,
    apply_qualifier,
    canonical_field,
    claim_fields_for,
    comparison_key,
    derive_title,
    detect_source_noise,
    detect_transformations,
    display_qualifier,
    normalize_artist_name,
    normalize_bpm,
    normalize_date,
    normalize_for_comparison,
    normalize_key,
    normalize_text,
    parse_artists,
    parse_date,
)
from trackforge.domain.policies.registry import (
    DEFAULT_POLICIES,
    DEFAULT_POLICY_REGISTRY,
    FieldPolicyRegistry,
    PolicyRegistry,
    build_default_registry,
    default_registry,
    policy_version,
)
from trackforge.domain.policies.strategies import (
    StrategyResult,
    select_consensus,
    select_derive,
    select_keep_existing,
    select_review,
    select_source_priority,
    select_unknown,
    select_user_lock,
)
from trackforge.domain.policies.validators import (
    Severity,
    ValidationIssue,
    ValidationResult,
    merge_results,
    validate_claim,
    validate_confidence,
    validate_cross_fields,
    validate_field,
    validate_values,
)

__all__ = [
    "DEFAULT_POLICIES",
    "DEFAULT_POLICY_REGISTRY",
    "ArtistCredit",
    "ParsedDate",
    "FieldPolicyRegistry",
    "PolicyRegistry",
    "Severity",
    "StrategyResult",
    "ValidationIssue",
    "ValidationResult",
    "apply_qualifier",
    "build_default_registry",
    "canonical_field",
    "claim_fields_for",
    "comparison_key",
    "default_registry",
    "derive_title",
    "detect_source_noise",
    "detect_transformations",
    "display_qualifier",
    "merge_results",
    "normalization",
    "normalize_artist_name",
    "normalize_bpm",
    "normalize_date",
    "normalize_for_comparison",
    "normalize_key",
    "normalize_text",
    "parse_artists",
    "parse_date",
    "policy_version",
    "registry",
    "select_consensus",
    "select_derive",
    "select_keep_existing",
    "select_review",
    "select_source_priority",
    "select_unknown",
    "select_user_lock",
    "strategies",
    "validators",
    "validate_claim",
    "validate_confidence",
    "validate_cross_fields",
    "validate_field",
    "validate_values",
]
