"""Keep game rules, runtime orchestration and presentation independently usable."""

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "src/sixnimmt"


def imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


@pytest.mark.parametrize(
    "package, forbidden",
    [
        ("engine", ("sixnimmt.arena", "sixnimmt.analytics", "sixnimmt.persistence", "sixnimmt.terminal", "openai")),
        ("arena", ("sixnimmt.analytics", "sixnimmt.terminal", "sixnimmt.cli", "sixnimmt.application", "rich", "typer")),
        (
            "analytics",
            ("sixnimmt.arena.match", "sixnimmt.arena.execution", "sixnimmt.arena.sessions", "sixnimmt.arena.decisions"),
        ),
    ],
)
def test_package_respects_its_dependency_boundary(package: str, forbidden: tuple[str, ...]) -> None:
    violations = [
        f"{path.relative_to(SOURCE)} imports {name}"
        for path in (SOURCE / package).rglob("*.py")
        for name in imports(path)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ]
    assert violations == []


def test_application_workflow_does_not_import_presentation() -> None:
    assert not any(
        name in ("typer", "rich") or name.startswith("sixnimmt.terminal") for name in imports(SOURCE / "application.py")
    )
