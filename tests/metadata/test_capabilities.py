"""Tests for capability and write-policy value objects."""

from __future__ import annotations

import pytest

from trackforge.metadata import (
    Capabilities,
    ContainerFormat,
    FieldCapability,
    TagSystem,
    UnsupportedPolicy,
    WritePolicy,
    WriteReport,
)
from trackforge.metadata.capabilities import standard_capabilities


def test_standard_capabilities_include_selected_groups() -> None:
    capabilities = standard_capabilities(
        ContainerFormat.MP4,
        TagSystem.MP4_ILST,
        audio=True,
        video=True,
        native_keys={"title": "©nam"},
    )

    assert capabilities.supports("title")
    assert capabilities.supports("album")
    assert capabilities.supports("show")
    title = capabilities.field("title")
    assert title is not None
    assert title.native_key == "©nam"
    assert capabilities.writable is False


def test_capabilities_can_hide_video_fields() -> None:
    capabilities = standard_capabilities(
        ContainerFormat.MP3,
        TagSystem.ID3V2,
        audio=True,
        video=False,
    )

    assert capabilities.supports("album")
    assert not capabilities.supports("season")


def test_missing_field_is_not_supported() -> None:
    capabilities = Capabilities(
        container=ContainerFormat.MP3,
        tag_system=TagSystem.ID3V2,
        fields={"title": FieldCapability()},
    )
    assert capabilities.supports("title")
    assert not capabilities.supports("lyrics")
    assert capabilities.field("lyrics") is None


def test_write_policy_defaults() -> None:
    policy = WritePolicy()
    assert policy.on_unsupported is UnsupportedPolicy.SKIP
    assert policy.write_legacy is False
    assert policy.preserve_unknown is True
    assert policy.verify is True
    assert policy.remove_missing is False
    assert policy.atomic is True
    assert policy.id3_version == 4
    assert policy.write_extra is False
    assert policy.remove_artwork is False


def test_write_report_lossy() -> None:
    report = WriteReport()
    assert report.lossy is False
    assert report.changed is False

    report.skipped.append(("lyrics", "not supported"))
    assert report.lossy is True


def test_write_policy_rejects_unknown_id3_version() -> None:
    with pytest.raises(ValueError):
        _ = WritePolicy(id3_version=5)
