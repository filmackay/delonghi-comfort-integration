"""Validate locally; supply --version X.Y.Z to validate GitHub CI and push its tag."""

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "filmackay/delonghi-comfort-integration"
HASSFEST_IMAGE = "ghcr.io/home-assistant/hassfest:latest"


class ReleaseError(Exception):
    """Stop the release before publishing an unvalidated tag."""


def run(*args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run an argument vector without a shell, from the repository root."""
    print("+ " + " ".join(args), flush=True)
    return subprocess.run(
        args, cwd=ROOT, check=check, text=True, capture_output=capture, timeout=1800,
    )


def normalize_version(value: str) -> str:
    version = value.removeprefix("v")
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise ReleaseError("Version must be X.Y.Z or vX.Y.Z, without leading zeroes or prerelease suffixes")
    return version


def check_versions(supplied: str | None) -> str:
    """Require the manifest, project, lockfile and latest changelog to agree."""
    manifest = json.loads((ROOT / "custom_components/delonghi_comfort/manifest.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    version = normalize_version(supplied if supplied is not None else manifest["version"])
    versions = {
        "manifest.json": manifest["version"],
        "pyproject.toml": project["version"],
    }
    packages = [package for package in lock["package"] if package["name"] == project["name"]]
    if len(packages) != 1:
        raise ReleaseError("uv.lock must contain exactly one entry for this project; run uv lock")
    versions["uv.lock"] = packages[0]["version"]
    for name, actual in versions.items():
        if actual != version:
            raise ReleaseError(f"{name} has version {actual!r}; expected {version!r}")
    changelog = (ROOT / "CHANGELOG.md").read_text()
    headings = re.findall(r"^##\s+(.+?)\s*$", changelog, re.MULTILINE)
    if not headings or headings[0] != version:
        raise ReleaseError(f"First CHANGELOG.md release heading must be '## {version}'")
    notes = re.split(r"^##\s+", changelog, flags=re.MULTILINE)[1].partition("\n")[2].strip()
    if not notes:
        raise ReleaseError("The changelog must contain release notes")
    return version


def check_git_state(expected_head: str | None = None) -> str:
    """Release only clean main commits already present on this fork's main."""
    remote = run("git", "remote", "get-url", "--push", "origin", capture=True).stdout.strip()
    valid_urls = {
        f"https://github.com/{REPOSITORY}", f"https://github.com/{REPOSITORY}.git",
        f"git@github.com:{REPOSITORY}.git", f"ssh://git@github.com/{REPOSITORY}.git",
    }
    if remote not in valid_urls:
        raise ReleaseError(f"origin's push URL must point to {REPOSITORY}")
    if run("git", "branch", "--show-current", capture=True).stdout.strip() != "main":
        raise ReleaseError("Check out main before releasing")
    if run("git", "status", "--porcelain", "--untracked-files=normal", capture=True).stdout:
        raise ReleaseError("Commit or remove working-tree changes and untracked files before releasing")
    head = run("git", "rev-parse", "HEAD", capture=True).stdout.strip()
    if expected_head is not None and head != expected_head:
        raise ReleaseError("HEAD changed during validation; run the release command again")
    remote_head = run("git", "ls-remote", "origin", "refs/heads/main", capture=True).stdout.split()
    if not remote_head or remote_head[0] != head:
        raise ReleaseError("Push this main commit with 'git push origin main' before releasing")
    return head


def check_unused_version(version: str) -> None:
    """Check local/remote tags and GitHub releases; fail closed on API errors."""
    tag = f"v{version}"
    if run("git", "tag", "--list", tag, version, capture=True).stdout.strip():
        raise ReleaseError(f"Version {version} already has a local tag")
    if run("git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{version}", capture=True).stdout.strip():
        raise ReleaseError(f"Version {version} already has a tag on GitHub")
    releases = run("gh", "api", "--paginate", f"repos/{REPOSITORY}/releases?per_page=100", "--jq", ".[].tag_name", capture=True).stdout.splitlines()
    if tag in releases or version in releases:
        raise ReleaseError(f"Version {version} already has a GitHub release")


def check_brand() -> None:
    """Check the local asset required by Marketplace's brands validation."""
    icon = ROOT / "custom_components/delonghi_comfort/brand/icon.png"
    if not icon.is_file():
        raise ReleaseError("Missing Marketplace brand asset: custom_components/delonghi_comfort/brand/icon.png")
    with Image.open(icon) as image:
        if image.format != "PNG" or image.size != (256, 256):
            raise ReleaseError("brand/icon.png must be a valid 256x256 PNG")
        image.verify()


def check_repository_settings() -> None:
    """Fail early on GitHub metadata that cannot be checked from local files."""
    data = json.loads(run(
        "gh", "api", f"repos/{REPOSITORY}", capture=True,
    ).stdout)
    missing = []
    if not (data.get("description") or "").strip():
        missing.append("add a repository description")
    if not data.get("has_issues"):
        missing.append("enable Issues")
    if not data.get("topics"):
        missing.append("add repository topics (for example home-assistant)")
    if data.get("archived"):
        missing.append("unarchive the repository")
    if missing:
        raise ReleaseError("Marketplace repository settings: " + "; ".join(missing))


def validate_locally(version: str) -> None:
    """Run local release gates against the current working tree."""
    for command in (
        ("uv", "sync", "--locked"),
        ("uv", "pip", "check"),
        ("uv", "run", "--frozen", "pyright"),
        ("uv", "run", "--frozen", "pytest"),
        ("uv", "run", "--frozen", "python", "-m", "compileall", "-q", "custom_components", "scripts", "tests", "discover.py"),
        ("docker", "run", "--rm", "--pull=always", "--platform", "linux/amd64", "-v", f"{ROOT}:/github/workspace:ro", HASSFEST_IMAGE),
        ("uv", "run", "--frozen", "python", "scripts/build_release.py", "--tag", f"v{version}"),
    ):
        run(*command)


def validate_on_github(head: str) -> None:
    """Require the full Validate workflow, including Marketplace checks, for HEAD."""
    def runs() -> list[dict]:
        return json.loads(run(
            "gh", "run", "list", "--repo", REPOSITORY, "--workflow", "validate.yml",
            "--commit", head, "--branch", "main", "--limit", "20",
            "--json", "databaseId,headSha,status,conclusion,createdAt,url", capture=True,
        ).stdout)

    candidates = runs()
    if not candidates:
        started = datetime.now(UTC).replace(microsecond=0)
        run("gh", "workflow", "run", "validate.yml", "--repo", REPOSITORY, "--ref", "main")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            candidates = [r for r in runs() if datetime.fromisoformat(r["createdAt"]) >= started]
            if candidates:
                break
            time.sleep(2)
    if not candidates:
        raise ReleaseError("No Validate run found for this commit; check GitHub Actions is enabled")
    latest = max(candidates, key=lambda r: (r["createdAt"], r["databaseId"]))
    if latest["headSha"] != head:
        raise ReleaseError("GitHub validation did not run against the release commit")
    if latest["status"] != "completed":
        try:
            run("gh", "run", "watch", str(latest["databaseId"]), "--repo", REPOSITORY, "--exit-status")
        except subprocess.CalledProcessError as err:
            raise ReleaseError(f"GitHub validation failed: {latest['url']}; inspect its failed job. No release tag was created.") from err
    elif latest["conclusion"] != "success":
        raise ReleaseError(f"GitHub validation failed: {latest['url']}")
    print(f"GitHub validation passed: {latest['url']}", flush=True)


def release(version: str | None) -> None:
    selected = check_versions(version)
    check_brand()
    for executable in ("uv", "docker", *(("git", "gh") if version is not None else ())):
        if not shutil.which(executable):
            raise ReleaseError(f"Install {executable} before running this command")
    head = None
    if version is not None:
        head = check_git_state()
        check_unused_version(selected)
        check_repository_settings()
    validate_locally(selected)
    if head is None:
        print("Local validation passed. No tag created or pushed. Supply --version to release.")
        return
    validate_on_github(head)
    # Recheck after potentially lengthy tests/CI, immediately before creating a tag.
    check_versions(selected)
    check_git_state(head)
    check_unused_version(selected)
    tag = f"v{selected}"
    run("git", "tag", "-a", tag, head, "-m", f"Release {selected}")
    try:
        run("git", "push", "origin", f"refs/tags/{tag}:refs/tags/{tag}")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
        raise ReleaseError(
            f"Tag {tag} exists locally; its push failed or could not be confirmed. "
            "Check GitHub before retrying; the script will not delete or overwrite tags."
        ) from err
    print(f"Pushed {tag}. GitHub Release workflow will validate again and publish: https://github.com/{REPOSITORY}/actions")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Release X.Y.Z (or vX.Y.Z); omit to run local validation only")
    args = parser.parse_args()
    try:
        release(args.version)
    except (ReleaseError, ValueError, KeyError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
        if isinstance(err, subprocess.CalledProcessError) and err.stderr:
            print(err.stderr.strip(), file=sys.stderr)
        print(f"Release stopped: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
