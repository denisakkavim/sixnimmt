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

Run the relevant tests while iterating, then the default suite and quality checks:

```bash
uv run pytest
make check
```

`make check` checks lockfile consistency, runs pre-commit hooks, and checks types.
Some hooks format files automatically; review their changes before committing.
To run formatting, linting, and type checks individually:

```bash
uv run ruff format --check .
uv run ruff check . --no-fix
uv run ty check
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
