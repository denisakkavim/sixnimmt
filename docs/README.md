# Documentation

This project runs deterministic 6 nimmt! games and experiments with trusted,
in-process bots. The distribution and Python import package are named `sixnimmt`.
The available CLI commands are `arena`, `replay`, and `summarise`.

Start with [Getting started](getting-started.md) to run a match and inspect its
trace. Then use the following guides:

| Guide | What it covers |
| --- | --- |
| [Using the Python package](python-api.md) | Installation in another project, runners, results, configuration, and direct engine control |
| [Game rules and information](game-rules.md) | Cards, placement, scoring, communication, and what each player can see |
| [Running arenas](arena.md) | Player files, Python API, CLI options, scheduling, limits, and outcomes |
| [Writing bots](bots.md) | Bot contract, registration, rejection feedback, and atomic action batches |
| [Strategy catalogue](strategy-families.md) | Baseline heuristics and proposed experimental strategies |
| [Probabilistic gameplay model](uncertainty-model.md) | Uniform-deal priors, learned opponent policies and random-choice probabilities, posterior inference, and candidate evaluation |
| [LLM players](llm-players.md) | Endpoint configuration, prompts, tools, private memory, and deadlines |
| [Traces and replay](traces.md) | Event/action/model logs, manifests, determinism, recovery, and analytics |
| [Architecture and development](development.md) | Module responsibilities, engine APIs, tests, and contribution workflow |

Examples assume commands run from the repository root. Configuration defaults
are documented alongside their source files so changes can be checked directly.
