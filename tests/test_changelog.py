from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "changelog.sh"

CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Journal entries can be searched (#30).

### Dependencies

- ruff 0.16.9 -> 0.16.10

## [1.4.2] - 2026-10-02

### Fixed

- A setting went from on -> off by itself.

[Unreleased]: https://github.com/ivenos/ha_caldav/compare/v1.4.2...HEAD
[1.4.2]: https://github.com/ivenos/ha_caldav/compare/v1.4.1...v1.4.2
"""

RUFF_11 = ("ruff", "0.16.10", "0.16.11")
RUFF_12 = ("ruff", "0.16.11", "0.16.12")


def run(tmp_path: Path, *args: str, text: str = CHANGELOG, data: str | None = None):
    file = tmp_path / "CHANGELOG.md"
    if not file.exists():
        file.write_text(text, encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "CHANGELOG": str(file)}
    if data is not None:
        (tmp_path / "data").write_text(data, encoding="utf-8")
        env["RENOVATE_POST_UPGRADE_COMMAND_DATA_FILE"] = str(tmp_path / "data")
    done = subprocess.run(
        ["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )
    return done, file.read_text(encoding="utf-8")


def unreleased(text: str) -> str:
    return text.split("## [Unreleased]\n", 1)[1].split("\n## [", 1)[0]


def git(repo: Path, *args: str) -> None:
    author = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run(
        ["git", *author, "-C", str(repo), *args], check=True, capture_output=True
    )


def merge(repo: Path, subject: str, *bumps: tuple[str, str, str]) -> None:
    for bump in bumps:
        run(repo, "dependency", *bump)
    git(repo, "commit", "-am", subject)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "CHANGELOG.md")
    git(tmp_path, "commit", "-m", "ci: Update dependency ruff to v0.16.10 (#9)")
    git(tmp_path, "tag", "v1.4.2")
    return tmp_path


def test_a_new_dependency_gets_a_line_of_its_own(tmp_path) -> None:
    done, text = run(tmp_path, "dependency", "hypothesis", "6.168.3", "6.169.0")

    assert done.returncode == 0
    assert unreleased(text).endswith(
        "- ruff 0.16.9 -> 0.16.10\n- hypothesis 6.168.3 -> 6.169.0\n"
    )


def test_a_dependency_bumped_again_keeps_the_version_it_started_from(tmp_path) -> None:
    _, text = run(tmp_path, "dependency", "ruff", "0.16.10", "0.16.11")

    assert "- ruff 0.16.9 -> 0.16.11\n" in text
    assert text.count("- ruff ") == 1


def test_a_note_on_a_line_does_not_outlive_the_version_it_was_about(tmp_path) -> None:
    text = CHANGELOG.replace("0.16.10\n", "0.16.10 (the formatter)\n")

    _, text = run(tmp_path, "dependency", "ruff", "0.16.10", "0.16.11", text=text)

    assert "- ruff 0.16.9 -> 0.16.11\n" in text


def test_the_pull_requests_on_a_line_outlive_a_bump(tmp_path) -> None:
    text = CHANGELOG.replace("0.16.10\n", "0.16.10 (#23, #25)\n")

    _, text = run(tmp_path, "dependency", "ruff", "0.16.10", "0.16.11", text=text)

    assert "- ruff 0.16.9 -> 0.16.11 (#23, #25)\n" in text


def test_the_first_dependency_adds_the_section_at_the_end(tmp_path) -> None:
    text = CHANGELOG.replace("### Dependencies\n\n- ruff 0.16.9 -> 0.16.10\n\n", "")

    _, text = run(tmp_path, "dependency", "ruff", "0.16.9", "0.16.10", text=text)

    assert unreleased(text) == (
        "\n### Added\n\n- Journal entries can be searched (#30).\n"
        "\n### Dependencies\n\n- ruff 0.16.9 -> 0.16.10\n"
    )


def test_a_dependency_lands_in_a_changelog_with_nothing_unreleased(tmp_path) -> None:
    text = (
        CHANGELOG.split("### Added")[0]
        + "## [1.4.2]"
        + CHANGELOG.split("## [1.4.2]")[1]
    )

    _, text = run(tmp_path, "dependency", "ruff", "0.16.9", "0.16.10", text=text)

    assert unreleased(text) == "\n### Dependencies\n\n- ruff 0.16.9 -> 0.16.10\n"


def test_a_dependency_of_an_older_release_is_left_alone(tmp_path) -> None:
    text = CHANGELOG.replace(
        "### Fixed\n", "### Dependencies\n\n- caldav 2.1.0 -> 3.3.0\n\n### Fixed\n"
    )

    _, text = run(tmp_path, "dependency", "caldav", "3.3.0", "3.4.0", text=text)

    assert "- caldav 2.1.0 -> 3.3.0\n" in text
    assert "- caldav 3.3.0 -> 3.4.0\n" in unreleased(text)


def test_renovate_hands_its_updates_over_in_a_file(tmp_path) -> None:
    data = "ruff\t0.16.10\t0.16.11\nactions/checkout\tv7\tv8\npinned\t\t\n"

    done, text = run(tmp_path, "dependency", data=data)

    assert done.returncode == 0
    assert unreleased(text).endswith(
        "- ruff 0.16.9 -> 0.16.11\n- actions/checkout v7 -> v8\n"
    )


def test_a_changelog_without_an_unreleased_section_is_refused(tmp_path) -> None:
    text = CHANGELOG.replace("## [Unreleased]", "## [1.5.0] - 2026-10-08")

    done, after = run(tmp_path, "dependency", "ruff", "1", "2", text=text)

    assert done.returncode == 1
    assert "no Unreleased section" in done.stderr
    assert after == text


def test_a_release_turns_unreleased_into_its_version(tmp_path) -> None:
    done, text = run(tmp_path, "release", "1.5.0", "2026-10-09")

    assert done.returncode == 0
    assert "## [Unreleased]\n\n## [1.5.0] - 2026-10-09\n\n### Added\n" in text
    assert (
        "[Unreleased]: https://github.com/ivenos/ha_caldav/compare/v1.5.0...HEAD\n"
        "[1.5.0]: https://github.com/ivenos/ha_caldav/compare/v1.4.2...v1.5.0\n"
        "[1.4.2]: " in text
    )


def test_the_first_release_links_to_its_tag(tmp_path) -> None:
    text = (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- Everything.\n\n"
        "[Unreleased]: https://github.com/ivenos/new/commits/main\n"
    )

    _, text = run(tmp_path, "release", "0.1.0", "2026-10-09", text=text)

    assert text.endswith(
        "[Unreleased]: https://github.com/ivenos/new/compare/v0.1.0...HEAD\n"
        "[0.1.0]: https://github.com/ivenos/new/releases/tag/v0.1.0\n"
    )


def test_a_release_names_the_pull_requests_behind_each_dependency(repo) -> None:
    merge(repo, "ci: Update dependency ruff to v0.16.11 (#27)", RUFF_11)
    merge(
        repo,
        "ci: Update GitHub Actions (#28)",
        ("actions/checkout", "v7", "v8"),
        ("actions/setup-python", "v7", "v8"),
    )
    merge(repo, "ci: Update dependency ruff to v0.16.12 (#30)", RUFF_12)
    merge(repo, "fix: follow Home Assistant", ("caldav", "3.3.0", "3.4.0"))

    done, text = run(repo, "release", "1.5.0", "2026-10-09")

    assert done.returncode == 0, done.stderr
    assert text.split("### Dependencies\n\n")[1].split("\n\n")[0] == (
        "- ruff 0.16.9 -> 0.16.12 (#27, #30)\n"
        "- actions/checkout v7 -> v8 (#28)\n"
        "- actions/setup-python v7 -> v8 (#28)\n"
        "- caldav 3.3.0 -> 3.4.0"
    )
    done, _ = run(repo, "notes", "1.5.0")
    assert "- ruff 0.16.9 → 0.16.12 (#27, #30)\n" in done.stdout


def test_a_release_keeps_the_pull_requests_a_line_names_already(repo) -> None:
    named = CHANGELOG.replace("0.16.10\n", "0.16.10 (#23)\n")
    (repo / "CHANGELOG.md").write_text(named, encoding="utf-8")
    git(repo, "commit", "-am", "ci: Update dependency ruff to v0.16.10 (#23)")
    merge(repo, "ci: Update dependency ruff to v0.16.11 (#27)", RUFF_11)

    _, text = run(repo, "release", "1.5.0", "2026-10-09")

    assert "- ruff 0.16.9 -> 0.16.11 (#23, #27)\n" in text


def test_a_release_from_a_shallow_clone_is_refused(repo, tmp_path_factory) -> None:
    merge(repo, "ci: Update dependency ruff to v0.16.11 (#27)", RUFF_11)
    shallow = tmp_path_factory.mktemp("shallow")
    git(shallow, "clone", "-q", "--depth", "1", f"file://{repo}", ".")
    before = (shallow / "CHANGELOG.md").read_text(encoding="utf-8")

    done, after = run(shallow, "release", "1.5.0")

    assert done.returncode == 1
    assert "shallow" in done.stderr
    assert after == before


def test_a_release_without_the_tag_of_the_last_one_is_refused(repo) -> None:
    git(repo, "tag", "-d", "v1.4.2")

    done, after = run(repo, "release", "1.5.0")

    assert done.returncode == 1
    assert "v1.4.2" in done.stderr
    assert after == CHANGELOG


@pytest.mark.parametrize(
    ("version", "text", "reason"),
    [
        ("1.4.2", CHANGELOG, "already"),
        ("v1.5.0", CHANGELOG, "not a version"),
        ("1.5", CHANGELOG, "not a version"),
        (
            "1.5.0",
            CHANGELOG.split("### Added")[0] + "## [1.4.2] - 2026-10-02\n",
            "nothing",
        ),
    ],
)
def test_a_release_that_cannot_be_right_changes_nothing(
    tmp_path, version: str, text: str, reason: str
) -> None:
    done, after = run(tmp_path, "release", version, text=text)

    assert done.returncode == 1
    assert reason in done.stderr
    assert after == text


def test_the_notes_of_a_release_are_its_section_dressed_up(tmp_path) -> None:
    run(tmp_path, "release", "1.5.0", "2026-10-09")

    done, _ = run(tmp_path, "notes", "1.5.0")

    assert done.stdout == (
        "## ✨ Added\n\n- Journal entries can be searched (#30).\n\n"
        "## 📦 Dependencies\n\n- ruff 0.16.9 → 0.16.10\n\n"
        "**Full changelog:** [v1.4.2...v1.5.0]"
        "(https://github.com/ivenos/ha_caldav/compare/v1.4.2...v1.5.0)\n"
    )


def test_an_arrow_outside_the_dependencies_stays_as_it_was_written(tmp_path) -> None:
    done, _ = run(tmp_path, "notes", "1.4.2")

    assert "## 🐛 Fixed\n\n- A setting went from on -> off by itself.\n" in done.stdout


def test_notes_for_a_version_the_changelog_does_not_have_are_refused(tmp_path) -> None:
    done, _ = run(tmp_path, "notes", "9.9.9")

    assert done.returncode == 1
    assert "no section for 9.9.9" in done.stderr
    assert done.stdout == ""


def test_every_version_in_the_changelog_has_its_link_and_its_notes(tmp_path) -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d\d-\d\d$", text, re.M)

    assert versions
    assert re.findall(r"^\[(\d+\.\d+\.\d+)\]: https://", text, re.M) == versions
    assert "\n[Unreleased]: https://" in text
    for version in versions:
        done, _ = run(tmp_path, "notes", version, text=text)
        assert done.returncode == 0, version
        assert "\n## " in f"\n{done.stdout}", version


SECTIONS = [
    "Breaking changes",
    "Added",
    "Changed",
    "Deprecated",
    "Removed",
    "Fixed",
    "Security",
    "Dependencies",
]


def test_what_users_have_to_act_on_leads_the_notes(tmp_path) -> None:
    text = CHANGELOG.replace(
        "### Added\n",
        "### Breaking changes\n\n- The old option is gone.\n\n### Added\n",
    )
    run(tmp_path, "release", "2.0.0", "2026-10-09", text=text)

    done, _ = run(tmp_path, "notes", "2.0.0")

    assert done.stdout.startswith(
        "## 🚨 Breaking changes\n\n- The old option is gone.\n\n## ✨ Added\n"
    )


def test_the_changelog_keeps_to_its_sections_in_their_order() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    for release in text.split("\n## [")[1:]:
        found = re.findall(r"^### (.+)$", release, re.M)
        assert found == [name for name in SECTIONS if name in found], release[:20]
        between = release.split("\n", 1)[1].split("\n[Unreleased]: ")[0]
        for line in between.splitlines():
            assert line == "" or line.startswith(("### ", "- ")), line


def test_dependencies_are_written_with_a_plain_arrow() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "→" not in text
    for release in text.split("\n## [")[1:]:
        if "### Dependencies\n" not in release:
            continue
        listed = (
            release.split("### Dependencies\n")[1].split("\n## ")[0].split("\n[")[0]
        )
        for line in filter(None, listed.splitlines()):
            assert re.fullmatch(r"- \S+ \S+ -> \S+( \(.+\))?", line), line
