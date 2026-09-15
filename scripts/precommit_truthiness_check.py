"""Check staged changes for non-boolean truthiness during pre-commit.

The script finds conditions and asks ty to determine whether their values are
boolean. Checks run on temporary source copies, which are never executed.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path
from typing import Annotated

import typer

HELPER = "__sixnimmt_boolean_condition__"
app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


def condition_values(tree: ast.AST) -> list[ast.expr]:
    """Find values whose truth is tested, including short-circuit operands."""
    candidates: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.Assert, ast.IfExp)):
            candidates.append(node.test)
        elif isinstance(node, ast.comprehension):
            candidates.extend(node.ifs)
        elif isinstance(node, ast.match_case) and node.guard is not None:
            candidates.append(node.guard)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            candidates.append(node.operand)
        elif isinstance(node, ast.BoolOp):
            # Require boolean operands even for `value or default` assignments.
            candidates.extend(node.values)

    unique = {id(node): node for node in candidates}
    return [
        node
        for node in unique.values()
        if not isinstance(node, (ast.Compare, ast.BoolOp))
        and not (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not))
        and not (isinstance(node, ast.Constant) and isinstance(node.value, bool))
    ]


def module_header_end(tree: ast.Module) -> int:
    """Find the line after which imports and helper definitions may be inserted."""
    end = 0
    for index, statement in enumerate(tree.body):
        is_docstring = (
            index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
        is_future = isinstance(statement, ast.ImportFrom) and statement.module == "__future__"
        if not is_docstring and not is_future:
            break
        if statement.end_lineno is None:
            message = "Module header has no source location"
            raise ValueError(message)
        end = statement.end_lineno
    return end


def instrument(source: str, changed_lines: set[int] | None = None) -> tuple[str, dict[int, int]]:
    """Insert type probes and map generated lines back to the original source."""
    tree = ast.parse(source)
    helper = HELPER
    while helper in source:
        helper += "_"
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
    insertions: dict[int, list[str]] = {}
    for node in condition_values(tree):
        if node.end_lineno is None or node.end_col_offset is None:
            message = "Condition has no source location"
            raise ValueError(message)
        if changed_lines is not None and changed_lines.isdisjoint(range(node.lineno, node.end_lineno + 1)):
            continue
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        # The second operand retains the original condition and its narrowing.
        # Probes have no runtime semantics: these files only go through ty.
        expression = ast.unparse(node)
        insertions.setdefault(start, []).append(f"({helper}(({expression})) and (")
        insertions.setdefault(end, []).insert(0, "))")

    # Module-level conditions also need the helper to be defined before use.
    # Keep docstrings and future imports in their required positions.
    header_end = module_header_end(tree)
    helper_source = (
        "\nimport typing as __truthiness_typing\n"
        "import numpy as __truthiness_numpy\n"
        f"def {helper}(value: bool | __truthiness_numpy.bool_) -> __truthiness_typing.Literal[True]:\n"
        "    return True\n\n"
    )
    insertions.setdefault(offsets[header_end], []).insert(0, helper_source)

    raw = source.encode("utf-8")
    chunks: list[str] = []
    line_map = {1: 1}
    original_line = 1
    generated_line = 1
    previous = 0
    for offset in sorted(insertions):
        segment = raw[previous:offset].decode("utf-8")
        chunks.append(segment)
        for _ in range(segment.count("\n")):
            original_line += 1
            generated_line += 1
            line_map[generated_line] = original_line
        addition = "".join(insertions[offset])
        chunks.append(addition)
        for _ in range(addition.count("\n")):
            generated_line += 1
            line_map[generated_line] = original_line
        previous = offset
    chunks.append(raw[previous:].decode("utf-8"))
    transformed = "".join(chunks)
    ast.parse(transformed)
    return transformed, line_map


def python_files(project: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607 -- Git is a prerequisite.
        cwd=project,
        check=True,
        capture_output=True,
    )
    paths = {Path(name.decode()) for name in result.stdout.split(b"\0") if name.endswith((b".py", b".pyi"))}
    return sorted(path for path in paths if (project / path).is_file())


def check_paths(project: Path, paths: list[Path]) -> list[str]:
    """Return diagnostics; fail loudly if the checker cannot run."""
    project = project.resolve()
    paths = sorted({(project / path).resolve().relative_to(project) for path in paths})
    with tempfile.TemporaryDirectory(prefix="sixnimmt-truthiness-") as directory:
        snapshot = Path(directory)
        (snapshot / "src").mkdir()
        # Preserve package layouts and imports without touching the worktree.
        for relative in python_files(project):
            original = project / relative
            if original.is_file():
                destination = snapshot / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, destination)
        shutil.copyfile(project / "pyproject.toml", snapshot / "pyproject.toml")
        return check_snapshot(snapshot, paths)


def check_snapshot(snapshot: Path, paths: list[Path], changed_lines: dict[Path, set[int]] | None = None) -> list[str]:
    """Probe selected conditions while retaining complete files for type inference."""
    maps: dict[str, dict[int, int]] = {}
    for relative in paths:
        (snapshot / relative).resolve().relative_to(snapshot.resolve())
        with tokenize.open(snapshot / relative) as source_file:
            lines = None if changed_lines is None else changed_lines[relative]
            transformed, line_map = instrument(source_file.read(), lines)
        destination = snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(transformed, encoding="utf-8")
        maps[relative.as_posix()] = line_map

    result = subprocess.run(  # noqa: S603 -- Fixed local executable and argument list; no shell.
        [
            str(Path(sys.executable).with_name("ty")),
            "check",
            "--project",
            str(snapshot),
            "--python",
            sys.prefix,
            "--extra-search-path",
            str(snapshot / "src"),
            "--extra-search-path",
            str(snapshot),
            "--output-format",
            "gitlab",
            "--ignore",
            "all",
            "--error",
            "invalid-argument-type",
            *[str(snapshot / path) for path in paths],
        ],
        cwd=snapshot,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        detail = result.stderr if result.stderr != "" else result.stdout
        message = f"ty failed: {detail}"
        raise RuntimeError(message)
    diagnostics = json.loads(result.stdout)
    failures = []
    for diagnostic in diagnostics:
        description = diagnostic["description"]
        if HELPER not in description:
            continue
        location = diagnostic["location"]
        path = Path(location["path"])
        if path.is_absolute():
            path = path.relative_to(snapshot)
        line = location["positions"]["begin"]["line"]
        original_line = maps[path.as_posix()][line]
        detail = description.split(" is incorrect: ", 1)[-1]
        failures.append(f"{path}:{original_line}: explicit-truthiness: {detail}")
    return sorted(set(failures))


DIFF_OPTIONS = ["--cached", "--no-ext-diff", "--no-textconv", "--no-color", "--find-renames"]


def git_output(project: Path, arguments: list[str], input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(  # noqa: S603 -- Git arguments are passed without a shell.
        ["git", *arguments],  # noqa: S607 -- Git is a prerequisite.
        cwd=project,
        input=input_bytes,
        check=True,
        capture_output=True,
    )
    return result.stdout


def staged_lines(project: Path) -> dict[Path, set[int]]:
    """Find added lines in index-versus-HEAD patches, retaining rename context."""
    entries = iter(
        git_output(project, ["diff", *DIFF_OPTIONS, "--name-status", "-z", "--diff-filter=AMR"]).split(b"\0")
    )
    changed: dict[Path, set[int]] = {}
    for status in entries:
        if status == b"":
            continue
        first = next(entries).decode()
        names = [first, next(entries).decode()] if status.startswith(b"R") else [first]
        path = Path(names[-1])
        if path.suffix not in (".py", ".pyi"):
            continue
        patch = git_output(
            project, ["diff", *DIFF_OPTIONS, "--unified=0", "--", *[f":(literal){name}" for name in names]]
        )
        lines: set[int] = set()
        for match in re.finditer(rb"^@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,([0-9]+))? @@", patch, re.MULTILINE):
            start = int(match[1])
            count = 1 if match[2] is None else int(match[2])
            lines.update(range(start, start + count))
        if len(lines) > 0:
            changed[path] = lines
    return changed


def check_staged(project: Path) -> list[str]:
    """Check only conditions touched by staged changes, using index contents."""
    changed = staged_lines(project)
    if len(changed) == 0:
        return []
    with tempfile.TemporaryDirectory(prefix="sixnimmt-staged-truthiness-") as directory:
        snapshot = Path(directory)
        (snapshot / "src").mkdir()
        # Export the index, including unchanged imports. Unstaged and untracked
        # files cannot influence the check, even for partially staged files.
        tracked = git_output(project, ["ls-files", "--cached", "-z"]).split(b"\0")
        selected = [name for name in tracked if name.endswith((b".py", b".pyi")) or name == b"pyproject.toml"]
        git_output(
            project,
            ["checkout-index", f"--prefix={snapshot}/", "-z", "--stdin"],
            b"\0".join(selected) + b"\0",
        )
        return check_snapshot(snapshot, sorted(changed), changed)


@app.command(help=__doc__)
def main(
    paths: Annotated[list[Path] | None, typer.Argument(help="Working-tree Python files to check.")] = None,
    all_files: Annotated[
        bool, typer.Option("--all-files", help="Audit all working-tree Python files instead of the staged diff")
    ] = False,
) -> None:
    project = Path(__file__).resolve().parents[1]
    if all_files and paths is not None and len(paths) > 0:
        message = "Use either --all-files or explicit paths"
        raise typer.BadParameter(message)
    try:
        if all_files:
            failures = check_paths(project, python_files(project))
        elif paths is not None and len(paths) > 0:
            failures = check_paths(project, paths)
        else:
            failures = check_staged(project)
    except (OSError, ValueError, RuntimeError, SyntaxError, subprocess.SubprocessError) as error:
        typer.echo(f"explicit-truthiness: {error}", err=True)
        raise typer.Exit(code=2) from error
    for failure in failures:
        typer.echo(failure)
    if len(failures) > 0:
        typer.echo('Use an explicit comparison, such as `is not None`, `len(value) > 0`, or `value != ""`.')
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
