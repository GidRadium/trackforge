"""Tests for the staging area and collision handling."""

# pyright: standard

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from trackforge.acquire.models import CollisionPolicy
from trackforge.acquire.staging import StagingArea, allocate_target


def touch(path: Path, content: str = "data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_job_dir_is_created_and_isolated(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging")
    job = staging.job_dir("job-1")
    assert job == tmp_path / "staging" / "job-1"
    assert job.is_dir()
    (job / "file.part").write_text("partial", encoding="utf-8")
    assert (job / "file.part").read_text(encoding="utf-8") == "partial"

    with pytest.raises(FileExistsError):
        staging.job_dir("job-1")


@pytest.mark.parametrize("job_id", ["", ".", "..", "../escape", "a/b", "a\\b"])
def test_job_dir_rejects_unsafe_components(tmp_path: Path, job_id: str) -> None:
    staging = StagingArea(tmp_path / "staging")
    with pytest.raises(ValueError):
        staging.job_dir(job_id)


def test_allocate_target_flat_when_free(tmp_path: Path) -> None:
    target = tmp_path / "Artist - Title.m4a"
    assert allocate_target(target) == target


def test_allocate_target_error(tmp_path: Path) -> None:
    target = touch(tmp_path / "Artist - Title.m4a")
    with pytest.raises(FileExistsError):
        allocate_target(target, CollisionPolicy.ERROR)


def test_allocate_target_skip(tmp_path: Path) -> None:
    target = touch(tmp_path / "Artist - Title.m4a")
    assert allocate_target(target, CollisionPolicy.SKIP) is None


def test_allocate_target_overwrite(tmp_path: Path) -> None:
    target = touch(tmp_path / "Artist - Title.m4a")
    assert allocate_target(target, CollisionPolicy.OVERWRITE) == target


def test_allocate_target_subfolder_uses_numbers_not_parentheses(tmp_path: Path) -> None:
    target = touch(tmp_path / "Artist - Title.m4a")

    first = allocate_target(target, CollisionPolicy.SUBFOLDER)
    assert first is not None
    assert first == tmp_path / "1" / "Artist - Title.m4a"
    assert not (tmp_path / "Artist - Title (1).m4a").exists()

    touch(first)
    second = allocate_target(target, CollisionPolicy.SUBFOLDER)
    assert second == tmp_path / "2" / "Artist - Title.m4a"


def test_allocate_target_accepts_string_policy(tmp_path: Path) -> None:
    target = touch(tmp_path / "file.txt")
    assert allocate_target(target, cast(CollisionPolicy, "skip")) is None


def test_commit_moves_file(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging")
    source = touch(staging.job_dir("job-1") / "media.m4a", "media")
    target = tmp_path / "out" / "Artist - Title.m4a"

    committed = staging.commit(source, target)
    assert committed == target
    assert target.read_text(encoding="utf-8") == "media"
    assert not source.exists()


def test_commit_skip_leaves_source(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging")
    source = touch(staging.job_dir("job-1") / "media.m4a")
    target = touch(tmp_path / "out" / "Artist - Title.m4a")

    assert staging.commit(source, target, policy=CollisionPolicy.SKIP) is None
    assert source.exists()
    assert target.read_text(encoding="utf-8") == "data"


def test_commit_overwrite_replaces_target(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging")
    source = touch(staging.job_dir("job-1") / "media.m4a", "new")
    target = touch(tmp_path / "out" / "media.m4a", "old")

    committed = staging.commit(source, target, policy=CollisionPolicy.OVERWRITE)
    assert committed == target
    assert target.read_text(encoding="utf-8") == "new"


def test_commit_group_moves_sidecars_into_same_folder(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging")
    job = staging.job_dir("job-1")
    media = touch(job / "Artist - Title.m4a", "media")
    lyrics = touch(job / "Artist - Title.lrc", "lyrics")
    info = touch(job / "Artist - Title.info.json", "{}")
    target = tmp_path / "out" / "Artist - Title.m4a"

    committed = staging.commit_group(media, [lyrics, info], target)
    assert committed == target
    assert target.read_text(encoding="utf-8") == "media"
    assert (target.parent / "Artist - Title.lrc").read_text(encoding="utf-8") == "lyrics"
    assert (target.parent / "Artist - Title.info.json").read_text(encoding="utf-8") == "{}"
    assert not media.exists()
    assert not lyrics.exists()
    assert not info.exists()


def test_commit_group_uses_one_subfolder_for_all_members(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging")
    job = staging.job_dir("job-1")
    target = tmp_path / "out" / "media.m4a"

    first_media = touch(job / "media.m4a", "first")
    first_sidecar = touch(job / "media.lrc", "first lyrics")
    assert staging.commit_group(first_media, [first_sidecar], target) == target

    second_media = touch(job / "media.m4a", "second")
    second_sidecar = touch(job / "media.lrc", "second lyrics")
    committed = staging.commit_group(second_media, [second_sidecar], target)

    assert committed == tmp_path / "out" / "1" / "media.m4a"
    assert (tmp_path / "out" / "1" / "media.m4a").read_text(encoding="utf-8") == "second"
    assert (
        tmp_path / "out" / "1" / "media.lrc"
    ).read_text(encoding="utf-8") == "second lyrics"


def test_commit_group_sidecar_collision_alone_uses_subfolder(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging")
    job = staging.job_dir("job-1")
    media = touch(job / "media.m4a", "media")
    sidecar = touch(job / "media.lrc", "lyrics")
    target = tmp_path / "out" / "media.m4a"
    touch(tmp_path / "out" / "media.lrc", "old lyrics")

    committed = staging.commit_group(media, [sidecar], target)
    assert committed == tmp_path / "out" / "1" / "media.m4a"
    assert (tmp_path / "out" / "1" / "media.lrc").read_text(encoding="utf-8") == "lyrics"
    assert (tmp_path / "out" / "media.lrc").read_text(encoding="utf-8") == "old lyrics"


def test_commit_group_error_policy_is_atomic_enough(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging")
    job = staging.job_dir("job-1")
    media = touch(job / "media.m4a", "media")
    sidecar = touch(job / "media.lrc", "lyrics")
    target = touch(tmp_path / "out" / "media.m4a", "old")

    with pytest.raises(FileExistsError):
        staging.commit_group(media, [sidecar], target, policy=CollisionPolicy.ERROR)
    assert media.exists()
    assert sidecar.exists()
    assert target.read_text(encoding="utf-8") == "old"


def test_commit_group_skip_leaves_group_untouched(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging")
    job = staging.job_dir("job-1")
    media = touch(job / "media.m4a", "media")
    sidecar = touch(job / "media.lrc", "lyrics")
    target = touch(tmp_path / "out" / "media.m4a", "old")

    assert (
        staging.commit_group(media, [sidecar], target, policy=CollisionPolicy.SKIP)
        is None
    )
    assert media.exists()
    assert sidecar.exists()


def test_commit_group_missing_source_is_an_error(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging")
    job = staging.job_dir("job-1")
    media = touch(job / "media.m4a")
    with pytest.raises(FileNotFoundError):
        staging.commit_group(media, [job / "missing.lrc"], tmp_path / "out" / "media.m4a")


def test_staging_area_accepts_string_root(tmp_path: Path) -> None:
    staging = StagingArea(cast(Path, str(tmp_path / "staging")))
    assert staging.root == tmp_path / "staging"
