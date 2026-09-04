# sixnimmt-server

[![Release](https://img.shields.io/github/v/release/denisakkavim/sixnimmt-server)](https://img.shields.io/github/v/release/denisakkavim/sixnimmt-server)
[![Build status](https://img.shields.io/github/actions/workflow/status/denisakkavim/sixnimmt-server/main.yml?branch=main)](https://github.com/denisakkavim/sixnimmt-server/actions/workflows/main.yml?query=branch%3Amain)
[![codecov](https://codecov.io/gh/denisakkavim/sixnimmt-server/branch/main/graph/badge.svg)](https://codecov.io/gh/denisakkavim/sixnimmt-server)
[![Commit activity](https://img.shields.io/github/commit-activity/m/denisakkavim/sixnimmt-server)](https://img.shields.io/github/commit-activity/m/denisakkavim/sixnimmt-server)
[![License](https://img.shields.io/github/license/denisakkavim/sixnimmt-server)](https://img.shields.io/github/license/denisakkavim/sixnimmt-server)

A game server and rules engine for deterministic 6 nimmt! matches.

- **Github repository**: <https://github.com/denisakkavim/sixnimmt-server/>
- **Documentation** <https://denisakkavim.github.io/sixnimmt-server/>

## Getting started with your project

### 1. Create a New Repository

First, create a repository on GitHub with the same name as this project, and then run the following commands:

```bash
git init -b main
git add .
git commit -m "init commit"
git remote add origin git@github.com:denisakkavim/sixnimmt-server.git
git push -u origin main
```

### 2. Set Up Your Development Environment

Then, install the environment and the pre-commit hooks with

```bash
make install
```

This will also generate your `uv.lock` file

### 3. Run the pre-commit hooks

Initially, the CI/CD pipeline might be failing due to formatting issues. To resolve those run:

```bash
uv run pre-commit run -a
```

### 4. Commit the changes

Lastly, commit the changes made by the two steps above to your repository.

```bash
git add .
git commit -m 'Fix formatting issues'
git push origin main
```

You are now ready to start development on your project!
The CI/CD pipeline will be triggered when you open a pull request, merge to main, or when you create a new release.

To finalize the set-up for publishing to PyPI, see [here](https://fpgmaas.github.io/cookiecutter-uv/features/publishing/#set-up-for-pypi).
For activating the automatic documentation with MkDocs/Zensical, see [here](https://fpgmaas.github.io/cookiecutter-uv/features/docs_tool/#deploying-to-github-pages).
To enable the code coverage reports, see [here](https://fpgmaas.github.io/cookiecutter-uv/features/codecov/).

## Releasing a new version



---

Repository initiated with [osprey-oss/cookiecutter-uv](https://github.com/osprey-oss/cookiecutter-uv).
