# Contributing to sixnimmt

Use the repository's issue tracker for bug reports and focused feature proposals.
For bugs, include the Python version, operating system, reproduction command,
seed, player configuration, and expected versus actual outcome. For model-related
problems, also include the endpoint type, model, timeout settings, and relevant
sanitized errors. Trace files contain private observations and model output;
remove credentials and sensitive content before sharing them.

## Set up development

Install Git, Python 3.13 or newer, and `uv`. Clone your fork using its GitHub clone
URL, then run these commands from the repository root:

```bash
uv sync --all-groups
uv run pre-commit install
git switch -c fix/describe-the-change
```

`uv sync` creates or updates `.venv`; use `uv run` to execute tools in it.
Read [AGENTS.md](AGENTS.md) for Python and test style, and
[the architecture guide](docs/development.md) for the source layout.

## Make and check changes

Keep changes focused. Add or adapt behavior tests when changing functionality;
documentation-only changes generally need link and example checks instead.
Preserve deterministic seed vectors and legacy replay fixtures unless the change
deliberately changes that compatibility.

Ruff and `ty` are the project's lint/type-checking stack. The locked `ty` version
reports `redundant-condition`, `missing-type-argument`,
`unsound-return-statement`, and `unsound-assignment` as errors, so provably constant conditions such as
an uncalled function block pre-commit and CI checks. Tuple conditions/assertions
and unsupported boolean conversions are also checked.
Do not enable rules such as Ruff `PLC1901` that encourage implicit truthiness.
Use absolute imports throughout the project, including imports between sibling
modules; Ruff `TID252` rejects all relative imports. Keep `TYPE_CHECKING` imports
with the other imports at the top of the file.

The local `explicit-truthiness` hook additionally rejects typed non-boolean values
in conditions, assertions, comprehension filters, match guards, `not`, and
`and`/`or` expressions (including fallbacks). Boolean variables, predicates,
comparisons, and NumPy boolean scalars are allowed.

```python
if options.enabled:
    ...  # Allowed: enabled is bool.
if len(options.names) > 0:
    ...  # Allowed: explicit collection check.
if options.names:
    ...  # Rejected: names is list[str].
names = options.names or []  # Rejected: implicit fallback.
```

Run `uv run pre-commit run explicit-truthiness` after staging changes to try it.
The hook checks conditions whose expression spans overlap added or modified lines
in the staged Python diff. Unchanged conditions, deleted lines, and pure renames
are skipped. A multiline expression can be reported at its start even when only
a later line changed. Changing an annotation alone does not recheck unchanged uses.

It reads the Git index, so unstaged edits and untracked files cannot affect the
result. Complete staged files and imports provide type context, but only changed
conditions receive boolean-parameter probes. The locked `ty` executable checks
temporary copies; the hook never executes those copies or edits source files.

For an optional full working-tree audit, run
`uv run python scripts/precommit_truthiness_check.py --all-files` (or pass specific paths).
Pre-commit's own `--all-files` flag does not expand this hook beyond the staged diff.

This is a prototype with limits: `Any` and unresolved types can pass, as can
unreachable code that `ty` does not inspect. Explicit `bool(...)` conversions and
truth testing inside `any(...)`/`all(...)` are outside its scope. Existing type
suppression comments also apply. The regular `ty` hook still checks the original
files for type errors. The adapter depends on `ty`'s diagnostic format and should
be tested when upgrading `ty`.

Run the relevant tests while iterating, then the default suite and quality checks:

```bash
uv run pytest
make check
```

`make check` checks lockfile consistency, runs pre-commit hooks, checks types, and
checks positive/negative static API fixtures. The fixtures under `tests/typing`
use `.py.txt` names so intentional errors do not enter the normal type check.
Each negative case must produce the expected diagnostic on its marked line;
missing and unexpected diagnostics both fail.
Some hooks format files automatically; review their changes before committing.
To run formatting, linting, and type checks individually:

```bash
uv run ruff format --check .
uv run ruff check . --no-fix
uv run ty check
uv run python scripts/check_typing.py
```

`make test` runs the default suite with coverage. Both it and `uv run pytest`
exclude `arena_slow`. For changes affecting gameplay, scheduling, determinism,
or information boundaries, run the relevant volume tests as well:

```bash
uv run pytest -m arena_slow
```

To run all tests in one invocation:

```bash
uv run pytest -o addopts=''
```

The volume suite checks 8,000 matches across classic and communication modes and
can take tens of minutes. Do not describe a default-suite pass as a full-suite pass.

CI currently tests Python 3.13 and invokes pytest and type checking directly,
with a separate quality job running `make check`. Its pytest command also excludes
the volume tests. The optional tox configuration targets Python 3.13 only:

```bash
uv run tox
```

When changing dependencies, use `uv add` or `uv remove` and commit the updated
`pyproject.toml` and `uv.lock` together.

## Documentation and pull requests

Update the relevant guide in [docs/](docs/README.md) when behavior or configuration
changes. Keep README focused on the overview and quick start. Check local links
and run any changed examples; avoid duplicating detailed reference tables across
documents.

Use a concise Conventional Commit message:

```text
fix(arena): preserve outcome after timeout
docs(bots): explain atomic action batches
```

Stage the intended files, review the staged diff, commit, and push your branch.
The pull request should explain the problem, resulting behavior, and validation
performed, including whether volume tests ran. Mention compatibility changes and
any remaining limitations. Code, tests, comments, and commit messages should be
understandable without historical planning documents.
