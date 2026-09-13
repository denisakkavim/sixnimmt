"""Exercise the hook against the actual project type checker."""

import subprocess
import textwrap
from pathlib import Path

import pytest

from scripts.precommit_truthiness_check import check_paths, check_staged, instrument


@pytest.fixture
def hook_project(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)  # noqa: S603, S607 -- Isolated test repository.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "hook-test"\nversion = "0"\n')
    return tmp_path


@pytest.mark.parametrize(
    "statement",
    [
        "if value: pass",
        "while value: break",
        "assert value",
        "result = 1 if value else 2",
        "result = [item for item in [1] if value]",
        "result = not value",
        "result = value or []",
        "result = value and True",
        "match value:\n    case _ if value: pass",
        "if (selected := value): pass",
    ],
)
def test_rejects_collection_truthiness(hook_project: Path, statement: str) -> None:
    source = "def example(value: list[int]) -> None:\n" + textwrap.indent(statement, "    ") + "\n"
    (hook_project / "example.py").write_text(source)

    failures = check_paths(hook_project, [Path("example.py")])

    assert len(failures) >= 1
    assert all("explicit-truthiness" in failure for failure in failures)
    assert any("list[int]" in failure for failure in failures)


@pytest.mark.parametrize("annotation", ["str", "int", "float", "object", "bool | None", "dict[str, int]"])
def test_rejects_non_boolean_types(hook_project: Path, annotation: str) -> None:
    (hook_project / "example.py").write_text(f"def example(value: {annotation}) -> None:\n    if value: pass\n")

    assert len(check_paths(hook_project, [Path("example.py")])) == 1


def test_accepts_booleans_and_preserves_short_circuit_narrowing(hook_project: Path) -> None:
    (hook_project / "example.py").write_text(
        textwrap.dedent("""\
        from typing import TypeGuard
        import numpy as np

        def is_text(value: object) -> TypeGuard[str]:
            return isinstance(value, str)

        def example(flag: bool, other: bool, value: str | None, unknown: object) -> None:
            if flag: pass
            while flag: break
            assert flag
            result = flag and not other
            result = flag or other
            result = True if flag else False
            items = [item for item in [1] if flag]
            if value is not None and value.startswith("x"): pass
            if is_text(unknown) and unknown.startswith("x"): pass
            if np.array([True]).all(): pass
            if len(items) > 0: pass
            if value != "": pass
            if True: pass
    """)
    )

    assert check_paths(hook_project, [Path("example.py")]) == []


def test_resolves_imported_predicates_and_pydantic_fields(hook_project: Path) -> None:
    (hook_project / "model.py").write_text(
        textwrap.dedent("""\
        from pydantic import BaseModel

        class Options(BaseModel):
            enabled: bool
            names: list[str]

        def enabled() -> bool:
            return True
    """)
    )
    (hook_project / "example.py").write_text(
        textwrap.dedent("""\
        from model import Options, enabled

        def example(options: Options) -> None:
            if enabled(): pass
            if options.enabled: pass
            if options.names: pass
    """)
    )
    original = (hook_project / "example.py").read_bytes()

    failures = check_paths(hook_project, [Path("example.py")])

    assert len(failures) == 1
    assert "example.py:6:" in failures[0]
    assert (hook_project / "example.py").read_bytes() == original


def test_reports_original_line_after_multiline_and_unicode_expressions(hook_project: Path) -> None:
    (hook_project / "example.py").write_text(
        textwrap.dedent("""\
        def example(names: list[str], flag: bool) -> None:
            café = "☕"
            if (
                names
            ): pass
            if flag and names: pass
    """)
    )

    failures = check_paths(hook_project, [Path("example.py")])

    assert len(failures) == 2
    assert "example.py:4:" in failures[0]
    assert "example.py:6:" in failures[1]


def test_untyped_values_remain_a_documented_gap(hook_project: Path) -> None:
    (hook_project / "example.py").write_text("def example(value):\n    if value: pass\n")

    assert check_paths(hook_project, [Path("example.py")]) == []


def test_uses_type_guard_narrowing_for_the_next_operand(hook_project: Path) -> None:
    (hook_project / "example.py").write_text(
        textwrap.dedent("""\
        from typing import TypeGuard

        def is_names(value: object) -> TypeGuard[list[str]]:
            return isinstance(value, list)

        def example(value: object) -> None:
            if is_names(value) and value: pass
    """)
    )

    failures = check_paths(hook_project, [Path("example.py")])

    assert len(failures) == 1
    assert "found `list[str]`" in failures[0]


def test_rejects_invalid_python_without_silently_skipping_it() -> None:
    with pytest.raises(SyntaxError):
        instrument("if :")


def test_absolute_paths_only_change_the_temporary_copy(hook_project: Path) -> None:
    path = hook_project / "example.py"
    source = "def example(value: str):\n    if value: pass\n"
    path.write_text(source)

    failures = check_paths(hook_project, [path])

    assert len(failures) == 1
    assert path.read_text() == source


def test_rejects_paths_outside_the_project(hook_project: Path) -> None:
    with pytest.raises(ValueError, match="not in the subpath"):
        check_paths(hook_project, [Path("../outside.py")])


def git(project: Path, *arguments: str) -> None:
    subprocess.run(  # noqa: S603 -- Argument list in an isolated test repository.
        ["git", *arguments],  # noqa: S607 -- Git is a prerequisite.
        cwd=project,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def committed_project(hook_project: Path) -> Path:
    (hook_project / "example.py").write_text(
        "def example(names: list[str], enabled: bool) -> None:\n"
        "    if names: pass\n"
        "    marker = 1\n"
        "    if enabled: pass\n"
    )
    git(hook_project, "config", "user.name", "Hook Test")
    git(hook_project, "config", "user.email", "hook@example.invalid")
    git(hook_project, "add", "pyproject.toml", "example.py")
    git(hook_project, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "test baseline")
    return hook_project


def test_skips_unchanged_violations_in_a_modified_file(committed_project: Path) -> None:
    path = committed_project / "example.py"
    path.write_text(path.read_text().replace("marker = 1", "marker = 2"))
    git(committed_project, "add", "example.py")

    assert check_staged(committed_project) == []


@pytest.mark.parametrize("staged_bad", [True, False])
def test_uses_staged_lines_and_contents_for_partial_staging(committed_project: Path, staged_bad: bool) -> None:
    path = committed_project / "example.py"
    baseline = path.read_text()
    staged = "if names: pass" if staged_bad else "if not enabled: pass"
    unstaged = "if enabled: pass" if staged_bad else "if names: pass"
    path.write_text(baseline.replace("if enabled: pass", staged))
    git(committed_project, "add", "example.py")
    path.write_text(baseline.replace("if enabled: pass", unstaged))

    failures = check_staged(committed_project)

    assert len(failures) == (1 if staged_bad else 0)
    if staged_bad:
        assert "example.py:4:" in failures[0]
    assert path.read_text() == baseline.replace("if enabled: pass", unstaged)


def test_ignores_untracked_and_unstaged_python_files(committed_project: Path) -> None:
    (committed_project / "untracked.py").write_text("if :")
    (committed_project / "example.py").write_text("if :")

    assert check_staged(committed_project) == []


def test_checks_an_added_file_before_the_first_commit(hook_project: Path) -> None:
    (hook_project / "example.py").write_text("def example(names: list[str]):\n    if names: pass\n")
    git(hook_project, "add", "pyproject.toml", "example.py")

    failures = check_staged(hook_project)

    assert len(failures) == 1
    assert "example.py:2:" in failures[0]


@pytest.mark.parametrize("remove_file", [True, False])
def test_skips_deleted_files_and_deletion_only_hunks(committed_project: Path, remove_file: bool) -> None:
    path = committed_project / "example.py"
    if remove_file:
        path.unlink()
    else:
        path.write_text(path.read_text().replace("    marker = 1\n", ""))
    git(committed_project, "add", "example.py")

    assert check_staged(committed_project) == []


@pytest.mark.parametrize("edit_condition", [False, True])
def test_only_checks_modified_conditions_in_renamed_files(committed_project: Path, edit_condition: bool) -> None:
    git(committed_project, "mv", "example.py", "renamed example.py")
    path = committed_project / "renamed example.py"
    if edit_condition:
        path.write_text(path.read_text().replace("if enabled: pass", "if names: pass"))
        git(committed_project, "add", "renamed example.py")

    failures = check_staged(committed_project)

    assert len(failures) == (1 if edit_condition else 0)
    if edit_condition:
        assert "renamed example.py:4:" in failures[0]


def test_checks_multiline_expression_when_only_a_later_line_changes(committed_project: Path) -> None:
    path = committed_project / "example.py"
    source = "def example(names: list[str]):\n    if sorted(\n        names, reverse=False\n    ): pass\n"
    path.write_text(source)
    git(committed_project, "add", "example.py")
    git(committed_project, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "multiline baseline")
    path.write_text(source.replace("reverse=False", "reverse=True"))
    git(committed_project, "add", "example.py")

    failures = check_staged(committed_project)

    assert len(failures) == 1
    assert "example.py:2:" in failures[0]


@pytest.mark.parametrize("staged_boolean", [True, False])
def test_resolves_import_types_from_the_index(committed_project: Path, staged_boolean: bool) -> None:
    model = committed_project / "model.py"
    boolean_source = "enabled: bool = True\n"
    string_source = 'enabled: str = "yes"\n'
    model.write_text(boolean_source if staged_boolean else string_source)
    (committed_project / "example.py").write_text("from model import enabled\nif enabled: pass\n")
    git(committed_project, "add", "example.py", "model.py")
    model.write_text(string_source if staged_boolean else boolean_source)

    failures = check_staged(committed_project)

    assert len(failures) == (0 if staged_boolean else 1)
