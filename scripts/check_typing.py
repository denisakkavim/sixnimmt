"""Check accepted and rejected type contracts without executing fixture code.

Fixtures in tests/typing end in .py.txt so normal type checks do not collect
intentional errors. Each expected diagnostic is marked on its source line with
``# expect: rule-name``. Missing and unexpected diagnostics both fail this check.
"""

import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

type DiagnosticKey = tuple[str, int, str]

EXPECTATION = re.compile(r"# expect: ([a-z][a-z-]*(?:, [a-z][a-z-]*)*)\s*$")


class Position(BaseModel):
    line: int


class Positions(BaseModel):
    begin: Position


class Location(BaseModel):
    path: str
    positions: Positions


class Diagnostic(BaseModel):
    check_name: str
    description: str
    location: Location


def expectations(path: Path, source: str) -> Counter[DiagnosticKey]:
    expected: Counter[DiagnosticKey] = Counter()
    for line_number, line in enumerate(source.splitlines(), start=1):
        if "# expect:" not in line:
            continue
        marker = EXPECTATION.search(line)
        if marker is None:
            message = f"{path.name}:{line_number}: malformed diagnostic expectation"
            raise ValueError(message)
        for rule in marker.group(1).split(", "):
            expected[(path.name, line_number, rule)] += 1
    return expected


def diagnostics(project: Path, paths: list[Path]) -> list[Diagnostic]:
    result = subprocess.run(  # noqa: S603 -- Fixed local checker, explicit arguments, no shell.
        [
            str(Path(sys.executable).with_name("ty")),
            "check",
            "--project",
            str(project),
            "--python",
            sys.prefix,
            "--extra-search-path",
            str(project / "src"),
            "--output-format",
            "gitlab",
            *[str(path) for path in paths],
        ],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        detail = result.stderr if result.stderr != "" else result.stdout
        message = f"ty failed: {detail}"
        raise RuntimeError(message)
    return TypeAdapter(list[Diagnostic]).validate_json(result.stdout)


def check_contracts(project: Path) -> tuple[list[str], int, int]:
    fixtures = sorted((project / "tests" / "typing").glob("*.py.txt"))
    if len(fixtures) == 0:
        message = "No typing fixtures found in tests/typing"
        raise ValueError(message)
    with tempfile.TemporaryDirectory(prefix="sixnimmt-typing-") as directory:
        expected: Counter[DiagnosticKey] = Counter()
        paths: list[Path] = []
        for fixture in fixtures:
            path = Path(directory) / fixture.stem
            source = fixture.read_text(encoding="utf-8")
            path.write_text(source, encoding="utf-8")
            paths.append(path)
            expected.update(expectations(path, source))
        observed = diagnostics(project, paths)
    actual: Counter[DiagnosticKey] = Counter(
        (Path(item.location.path).name, item.location.positions.begin.line, item.check_name) for item in observed
    )
    failures: list[str] = []
    for (filename, line, rule), count in sorted((expected - actual).items()):
        failures.append(f"{filename}:{line}: missing {rule} ({count})")
    for (filename, line, rule), count in sorted((actual - expected).items()):
        failures.append(f"{filename}:{line}: unexpected {rule} ({count})")
    return failures, len(fixtures), expected.total()


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    failures, fixture_count, diagnostic_count = check_contracts(project)
    if len(failures) > 0:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Typing contracts passed ({fixture_count} fixtures, {diagnostic_count} expected diagnostics).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
