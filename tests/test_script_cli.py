"""Run development-script entry points with their actual checker and exit codes."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture
def script_project(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)  # noqa: S603, S607 -- Isolated test repository.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "script-test"\nversion = "0"\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    for name in ("check_typing.py", "precommit_truthiness_check.py"):
        shutil.copyfile(SCRIPTS / name, tmp_path / "scripts" / name)
    return tmp_path


def invoke_script(project: Path, name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- Local script and explicit arguments in an isolated project.
        [sys.executable, str(project / "scripts" / name), *arguments],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("name", ["check_typing.py", "precommit_truthiness_check.py"])
def test_script_help_succeeds_without_running_checks(script_project: Path, name: str) -> None:
    result = invoke_script(script_project, name, "--help")

    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "--help" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("mode", ["staged", "paths", "all"])
def test_truthiness_script_reports_findings_on_stdout(script_project: Path, mode: str) -> None:
    (script_project / "example.py").write_text("def example(names: list[str]) -> None:\n    if names: pass\n")
    arguments: tuple[str, ...] = ()
    if mode == "staged":
        subprocess.run(
            ["git", "add", "pyproject.toml", "example.py"],  # noqa: S607 -- Stage an isolated test fixture.
            cwd=script_project,
            check=True,
        )
    elif mode == "paths":
        arguments = ("example.py",)
    else:
        arguments = ("--all-files",)

    result = invoke_script(script_project, "precommit_truthiness_check.py", *arguments)

    assert result.returncode == 1
    assert "example.py:2: explicit-truthiness:" in result.stdout
    assert "Use an explicit comparison" in result.stdout
    assert result.stderr == ""


def test_truthiness_script_defaults_to_staged_changes(script_project: Path) -> None:
    (script_project / "untracked.py").write_text("if :")

    result = invoke_script(script_project, "precommit_truthiness_check.py")

    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


def test_truthiness_script_rejects_conflicting_selection_modes(script_project: Path) -> None:
    result = invoke_script(script_project, "precommit_truthiness_check.py", "--all-files", "example.py")

    assert result.returncode == 2
    assert "Use either --all-files or explicit paths" in result.stderr
    assert result.stdout == ""


def test_truthiness_script_reports_checker_errors_on_stderr(script_project: Path) -> None:
    (script_project / "invalid.py").write_text("if :")

    result = invoke_script(script_project, "precommit_truthiness_check.py", "invalid.py")

    assert result.returncode == 2
    assert "explicit-truthiness: invalid syntax" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("source", "exit_code", "stdout", "stderr"),
    [
        (
            'value: int = "wrong"  # expect: invalid-assignment\n',
            0,
            "Typing contracts passed (1 fixtures, 1 expected diagnostics).\n",
            "",
        ),
        ("value: int = 1  # expect: invalid-assignment\n", 1, "", "example.py:1: missing invalid-assignment (1)\n"),
        ('value: int = "wrong"\n', 1, "", "example.py:1: unexpected invalid-assignment (1)\n"),
    ],
    ids=["accepted-contract", "missing-diagnostic", "unexpected-diagnostic"],
)
def test_typing_script_preserves_output_and_exit_codes(
    script_project: Path, source: str, exit_code: int, stdout: str, stderr: str
) -> None:
    fixtures = script_project / "tests" / "typing"
    fixtures.mkdir(parents=True)
    (fixtures / "example.py.txt").write_text(source)

    result = invoke_script(script_project, "check_typing.py")

    assert result.returncode == exit_code
    assert result.stdout == stdout
    assert result.stderr == stderr
