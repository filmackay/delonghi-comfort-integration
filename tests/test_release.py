"""Release validation must fail closed and never push an unchecked version."""

import json
import subprocess
from unittest.mock import MagicMock, call

import pytest

from scripts import release as target


@pytest.fixture
def metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(target, "ROOT", tmp_path)
    component = tmp_path / "custom_components/delonghi_comfort"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text('{"version":"1.1.0"}')
    (tmp_path / "pyproject.toml").write_text('[project]\nname="test"\nversion="1.1.0"\n')
    (tmp_path / "uv.lock").write_text('[[package]]\nname="test"\nversion="1.1.0"\n')
    (tmp_path / "CHANGELOG.md").write_text('# Changes\n\n## 1.1.0\n\nRelease notes.\n')
    return tmp_path


@pytest.mark.parametrize("version", ["1.1.0", "v1.1.0", None])
def test_matching_versions(metadata, version):
    assert target.check_versions(version) == "1.1.0"


@pytest.mark.parametrize("version", ["1.1", "1.1.0-rc1", "01.1.0", "1.1.0;echo x", "1.2.0"])
def test_bad_or_mismatched_version(metadata, version):
    with pytest.raises(target.ReleaseError):
        target.check_versions(version)


@pytest.mark.parametrize("filename", ["custom_components/delonghi_comfort/manifest.json", "pyproject.toml", "uv.lock", "CHANGELOG.md"])
def test_each_version_source_must_match(metadata, filename):
    path = metadata / filename
    path.write_text(path.read_text().replace("1.1.0", "1.0.0"))
    with pytest.raises(target.ReleaseError):
        target.check_versions("1.1.0")


def test_changelog_requires_notes(metadata):
    (metadata / "CHANGELOG.md").write_text('## 1.1.0')
    with pytest.raises(target.ReleaseError, match="release notes"):
        target.check_versions("1.1.0")


def output(text=""):
    return subprocess.CompletedProcess([], 0, stdout=text, stderr="")


@pytest.mark.parametrize("where", ["local", "remote", "release"])
def test_used_version_is_rejected(monkeypatch, where):
    run = MagicMock(side_effect=[output("v1.1.0" if where == "local" else ""), output("sha refs/tags/v1.1.0" if where == "remote" else ""), output("v1.1.0" if where == "release" else "")])
    monkeypatch.setattr(target, "run", run)
    with pytest.raises(target.ReleaseError, match="already"):
        target.check_unused_version("1.1.0")


def test_remote_lookup_failure_is_not_treated_as_unused(monkeypatch):
    monkeypatch.setattr(target, "run", MagicMock(side_effect=[output(), subprocess.CalledProcessError(128, "git ls-remote")]))
    with pytest.raises(subprocess.CalledProcessError):
        target.check_unused_version("1.1.0")


@pytest.mark.parametrize("change", ["wrong_remote", "wrong_branch", "dirty", "unpushed", "moved_head"])
def test_unsafe_git_state_rejected(monkeypatch, change):
    values = [
        "https://github.com/philm5/delonghi-comfort-integration" if change == "wrong_remote" else f"https://github.com/{target.REPOSITORY}",
        "feature" if change == "wrong_branch" else "main",
        " M file.py" if change == "dirty" else "",
        "new-head" if change == "moved_head" else "head",
        "other refs/heads/main" if change == "unpushed" else "head refs/heads/main",
    ]
    monkeypatch.setattr(target, "run", MagicMock(side_effect=[output(value) for value in values]))
    with pytest.raises(target.ReleaseError):
        target.check_git_state("head")


@pytest.fixture
def gates(monkeypatch):
    manager = MagicMock()
    for name, result in (("check_versions", "1.1.0"), ("check_brand", None), ("check_git_state", "head"), ("check_unused_version", None), ("check_repository_settings", None), ("validate_locally", None), ("validate_on_github", None), ("run", output())):
        mock = MagicMock(return_value=result)
        manager.attach_mock(mock, name)
        monkeypatch.setattr(target, name, mock)
    monkeypatch.setattr(target.shutil, "which", lambda executable: executable)
    return manager


@pytest.mark.parametrize("gate", ["check_versions", "check_brand", "check_git_state", "check_unused_version", "check_repository_settings", "validate_locally", "validate_on_github"])
def test_failed_gate_never_creates_or_pushes_tag(gates, gate):
    getattr(gates, gate).side_effect = target.ReleaseError("blocked")
    with pytest.raises(target.ReleaseError):
        target.release("1.1.0")
    gates.run.assert_not_called()


def test_no_version_never_touches_github_or_git_tags(gates):
    target.release(None)
    gates.validate_locally.assert_called_once()
    gates.check_git_state.assert_not_called()
    gates.check_unused_version.assert_not_called()
    gates.validate_on_github.assert_not_called()
    gates.run.assert_not_called()


def test_changed_tree_after_ci_prevents_tag(gates):
    gates.check_git_state.side_effect = ["head", target.ReleaseError("dirty")]
    with pytest.raises(target.ReleaseError):
        target.release("1.1.0")
    gates.run.assert_not_called()


def test_success_tags_exact_commit_only_after_every_gate(gates):
    target.release("1.1.0")
    assert gates.mock_calls == [
        call.check_versions("1.1.0"), call.check_brand(), call.check_git_state(), call.check_unused_version("1.1.0"), call.check_repository_settings(),
        call.validate_locally("1.1.0"), call.validate_on_github("head"),
        call.check_versions("1.1.0"), call.check_git_state("head"), call.check_unused_version("1.1.0"),
        call.run("git", "tag", "-a", "v1.1.0", "head", "-m", "Release 1.1.0"),
        call.run("git", "push", "origin", "refs/tags/v1.1.0:refs/tags/v1.1.0"),
    ]


def test_failed_push_keeps_local_tag_and_reports_uncertainty(gates):
    gates.run.side_effect = [output(), subprocess.CalledProcessError(1, "git push")]
    with pytest.raises(target.ReleaseError, match="could not be confirmed"):
        target.release("1.1.0")
    assert len(gates.run.call_args_list) == 2


def test_failed_github_validation_blocks_release(monkeypatch):
    run = {"databaseId": 1, "createdAt": "2026-11-10T00:00:00Z", "headSha": "head", "status": "completed", "conclusion": "failure", "url": "https://github.com/test/run"}
    command = MagicMock(return_value=output(json.dumps([run])))
    monkeypatch.setattr(target, "run", command)
    with pytest.raises(target.ReleaseError, match="GitHub validation failed"):
        target.validate_on_github("head")
    assert command.call_count == 1


def test_pending_ci_must_finish_successfully(monkeypatch):
    pending = {"databaseId": 1, "createdAt": "2026-11-10T00:00:00Z", "headSha": "head", "status": "in_progress", "url": "https://github.com/test/run"}
    command = MagicMock(side_effect=[output(json.dumps([pending])), subprocess.CalledProcessError(1, "gh run watch")])
    monkeypatch.setattr(target, "run", command)
    with pytest.raises(target.ReleaseError, match="No release tag was created"):
        target.validate_on_github("head")
    assert command.call_args.args == ("gh", "run", "watch", "1", "--repo", target.REPOSITORY, "--exit-status")


def test_wrong_commit_ci_is_rejected(monkeypatch):
    result = {"databaseId": 1, "createdAt": "2026-11-10T00:00:00Z", "headSha": "wrong", "status": "completed", "conclusion": "success", "url": "https://github.com/test/run"}
    monkeypatch.setattr(target, "run", MagicMock(return_value=output(json.dumps([result]))))
    with pytest.raises(target.ReleaseError, match="release commit"):
        target.validate_on_github("head")


def test_missing_ci_dispatches_validation_and_waits_for_exact_commit(monkeypatch):
    from datetime import UTC, datetime, timedelta

    result = {"databaseId": 1, "createdAt": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(), "headSha": "head", "status": "completed", "conclusion": "success", "url": "https://github.com/test/run"}
    command = MagicMock(side_effect=[output("[]"), output(), output(json.dumps([result]))])
    monkeypatch.setattr(target, "run", command)
    target.validate_on_github("head")
    assert command.call_args_list[1] == call("gh", "workflow", "run", "validate.yml", "--repo", target.REPOSITORY, "--ref", "main")


@pytest.mark.parametrize("missing", ["description", "has_issues", "topics"])
def test_repository_metadata_failure_is_clear(monkeypatch, missing):
    data = {"description": "Integration", "has_issues": True, "topics": ["home-assistant"], "archived": False}
    data.pop(missing)
    monkeypatch.setattr(target, "run", MagicMock(return_value=output(json.dumps(data))))
    with pytest.raises(target.ReleaseError, match="Marketplace repository settings"):
        target.check_repository_settings()


def test_valid_repository_metadata_passes(monkeypatch):
    data = {"description": "Integration", "has_issues": True, "topics": ["home-assistant"], "archived": False}
    monkeypatch.setattr(target, "run", MagicMock(return_value=output(json.dumps(data))))
    target.check_repository_settings()


def test_missing_brand_is_caught_locally(metadata):
    with pytest.raises(target.ReleaseError, match="brand/icon.png"):
        target.check_brand()


def test_brand_must_be_png_at_required_size(metadata):
    from PIL import Image
    brand = metadata / "custom_components/delonghi_comfort/brand"
    brand.mkdir()
    Image.new("RGBA", (32, 32)).save(brand / "icon.png")
    with pytest.raises(target.ReleaseError, match="256x256"):
        target.check_brand()
