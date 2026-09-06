# Tooling
* Use uv for Python environment and dependency management.

# Project Scope
* The distribution and Python import package are named `sixnimmt`.
* This project provides a deterministic 6 nimmt! rules engine and an in-process arena for trusted bots.
* The engine owns game rules and state transitions. Bot execution, scheduling, and operational limits belong in the arena.

# Documentation
* Start with `docs/README.md` for documentation and `CONTRIBUTING.md` for the development workflow.
* Update the relevant guide when changing public APIs, configuration, or observable behavior.
* Keep README.md focused on the overview and quick start.
* Code, tests, comments, and commit messages must be understandable without historical planning documents.

# Validation
* Run focused tests while iterating and the default suite before handing off code changes.
* `uv run pytest` excludes the `arena_slow` tests.
* For changes affecting gameplay, scheduling, determinism, or information boundaries, run the relevant volume tests as well.
* `uv run pytest -o addopts=''` runs all tests.
* Report which checks ran; distinguish default-suite results from full-suite results.
* For documentation-only changes, check links and run changed examples instead of rerunning unrelated tests.

# Commit Style
* Follow Conventional Commits: <type>(<scope>): <description>.
* Use imperative, concise descriptions.
* Use `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `build`, or `ci` as appropriate.
* Commit messages should be a brief one-line description of what the commit does.

## Python Style
* Optimize for clarity and explicitness, not cleverness or minimum line count.
* Prefer straightforward, explicit code over clever Python idioms when the latter reduce readability.
* Use intermediate variables when they make the code easier to understand; don't optimize them away just to reduce lines.
* Prefer comprehensions for simple transformations; use explicit loops when the logic becomes complex or harder to read.
* Don't rely on implicit truthiness when None, False, 0, or "" have different meanings.
* Prefer early returns / guard clauses over nested if statements. Keep nesting shallow.
* Keep the happy path unindented and easy to follow.
* Avoid deeply nested logic and large functions with multiple levels of branching.
* Keep functions focused on one responsibility and prefer small, cohesive functions over large ones.
* Avoid nested function definitions; prefer standalone functions or class methods unless nesting clearly makes the code easier to understand than the alternative.
* Use type hints for function parameters, return values, and important variables.
* Avoid unnecessary mutation; when mutation is useful, make it explicit and local.
* Comment why code exists or why an approach is necessary, not what straightforward code already does.
* Prefer a little duplication over an abstraction that makes the code harder to follow.
* Readable and explicit beats “Pythonic” and concise.

## Test Style
* Use `pytest` for tests.
* Keep each test function focused on testing one behaviour.
* Name tests descriptively, stating the behaviour or outcome being tested. Prefer names such as `test_rejects_expired_token` over generic names such as `test_validation`.
* Use `pytest.mark.parametrize` to test multiple cases of the same behaviour rather than duplicating test functions.
* Keep parametrised cases easy to read and give parameters descriptive names.
* Use fixtures for components or setup that should be reused across multiple tests.
* Reuse existing fixtures where sensible rather than creating duplicate fixtures or setup.
* Keep fixtures focused and avoid fixtures that perform too much unrelated setup.
* Prefer explicit test setup over excessive fixture indirection when the setup is specific to a single test.
* Tests should be independent and should not rely on execution order or state left by other tests.
* Prefer testing observable behaviour and outcomes over implementation details.
* Keep tests simple and readable; avoid abstractions or helper functions that make a test harder to understand.
* Avoid mocking unless it is necessary to isolate an external dependency or otherwise control behaviour that cannot reasonably be exercised directly.
* Tests should make the expected behaviour obvious to a reader without requiring them to understand the implementation.
