# ContractBench → Harbor Adapter

## Overview

This adapter converts ContractBench tasks into Harbor format.

- **Task types**: API observation-contract reasoning (agentic, server-in-the-loop)
- **Provenance**: [ContractBench](https://github.com/SecurityLab-UCD/contractbench) GitHub repository, paper [arXiv:2605.17281](https://arxiv.org/abs/2605.17281)
- **Dataset size**: 33 tasks (same as the original source benchmark)
- **Known constraints**: Requires network access to clone the public ContractBench repository, and Docker to build/run each task's FastAPI server environment.

## What is ContractBench?

ContractBench is a suite of **33 dual-axis observation-contract tasks** that test
whether an LLM agent can preserve *observation contracts* when interacting with a
local API. Each task starts a FastAPI server (auto-started by the environment's
Docker `ENTRYPOINT`) and gives the agent a natural-language contract describing
how it must call the API and record results. Tasks are scored along two axes:

- **Validity** — did the agent follow the prescribed protocol (correct endpoints,
  headers, ordering, auth flow, etc.)?
- **Integrity** — did the agent faithfully preserve the data it observed
  (no fabrication, correct hashing/anchoring, no shortcut injection)?

Task categories range from OAuth/token flows and webhook HMAC verification to
pagination integrity, idempotency, presigned URLs, saga consistency, and
adversarial shortcut-injection traps.

Each task contributes a single scalar reward in `[0, 1]` written to
`/logs/verifier/reward.txt` by a pytest verifier (`tests/test_outputs.py`).

**References**:
- Repository: https://github.com/SecurityLab-UCD/contractbench
- Paper: https://arxiv.org/abs/2605.17281

## Adapter Features

- All **33** ContractBench tasks. Upstream already ships each task in
  Harbor-native form under `harbor/cookbook-export/<task>/`; the adapter clones
  the public repo at a pinned ref (`c50eefee49b6925e2ccbf3c51a987ed705148725`,
  override with `--ref`) and copies each task directory.
- Changes applied on top of the copy, none of which alter the scoring logic:
  1. the provenance/canary footer is stripped from `instruction.md`;
  2. the benchmark name is replaced, in every agent-visible file and consistently
     in `tests/` and `solution/`, with the neutral name `ServicePortal` (same
     length, so string lengths and URL sizes are unchanged). Signing secrets that
     contain the name keep their exact bytes and are re-encoded as hex, because
     signatures derived from them are precomputed into the instructions;
  3. the verifier grades from a trusted copy of the grading helper in `tests/`
     instead of importing it from the agent-writable `/app/shared` package, and
     every task gets the hardened `tests/test.sh` from `task-template/`
     (absolute interpreter path, `PYTHONPATH` dropped, conftest discovery
     confined to `/tests`);
  4. `pytest` and `pytest-json-ctrf` are preinstalled in the image, so grading
     does not need network access at verify time;
  5. `task.toml` credits the benchmark authors.
- Oracle solutions pass **33/33** at reward 1.0 on the generated tasks.
- **Custom parity agent** (`src/contractbench/parity_agent.py`,
  `contractbench-parity-agent@1.0.0`). ContractBench's own harness drives the
  model with a native tool-calling loop, not a Harbor CLI agent, so parity uses a
  Harbor port of that loop. It matches the upstream harness on: the system prompt
  and single `bash` tool definition (copied verbatim from `agents/base.py`); the
  conversation (system prompt, then the task instruction); OpenAI chat
  completions with `tool_choice="auto"`, `temperature=0.0`, `max_tokens=16384`;
  at most 30 model calls; a soft deadline checked before each model call
  (upstream default 300 s; `--timeout`, 600 s in the parity runs);
  stopping on the first turn without tool calls; and `bash -c` per tool call
  with a 60 s limit, stdout then stderr, truncated at 30,000 characters. The
  agent runs on the host and calls the OpenAI API directly.
- Reward is a float in `[0, 1]`, so tasks are usable as RL/post-training reward
  signals.

## Generated Task Structure

```
datasets/contractbench/
├── {task_id}/                          # e.g. "basic-oauth-token"
│   ├── task.toml                       # Harbor task config (schema 1.3)
│   ├── instruction.md                  # Contract prompt (canary footer removed)
│   ├── environment/
│   │   ├── Dockerfile                  # python:3.12-slim, ENTRYPOINT starts FastAPI
│   │   ├── server.py                   # The task's API server
│   │   └── shared/                     # Shared helpers (crypto, oauth, logging, ...)
│   ├── solution/
│   │   └── solve.sh                    # Reference (oracle) solution
│   └── tests/
│       ├── test.sh                     # Verifier entrypoint → /logs/verifier/reward.txt
│       ├── test_outputs.py             # pytest validator
│       └── server_logging.py           # Trusted copy of the grading helper
```

Adapter code:

```
src/contractbench/
├── README.md
├── pyproject.toml                      # `contractbench` console script; `parity` extra
├── adapter_metadata.json
├── parity_experiment.json
├── contractbench.yaml                  # Default job config (oracle)
├── contractbench-parity.yaml           # Parity job config (custom agent, 3 runs)
└── src/contractbench/
    ├── adapter.py                      # ContractBenchAdapter
    ├── main.py                         # CLI: clone repo, then ContractBenchAdapter.run()
    ├── parity_agent.py                 # Harbor port of the upstream agent loop
    └── task-template/
        ├── tests/test.sh               # Installed into every task
        ├── task.toml                   # Source of the [task].authors written to every task
        └── environment/Dockerfile, instruction.md, solution/solve.sh
                                        # basic-oauth-token as generated (reference)
```

## Run Evaluation / Harness

All commands run from the repository root, after generating the tasks (see
below).

```bash
# Oracle on one task (should score 1.0)
uv run --project src/contractbench --extra parity \
  harbor run -p datasets/contractbench/basic-oauth-token -a oracle

# Oracle on the full dataset (default job config)
uv run --project src/contractbench --extra parity \
  harbor run -c src/contractbench/contractbench.yaml

# Any Harbor agent, e.g.
uv run --project src/contractbench --extra parity \
  harbor run -p datasets/contractbench -a codex -m "<model>"
```

The parity job is described under
[Comparison with Original Benchmark](#comparison-with-original-benchmark-parity).

## Usage: Create Task Directories

```bash
cd src/contractbench

# Clone the public repo at the pinned ref and emit all 33 tasks
uv run contractbench --output-dir ../../datasets/contractbench

# Or use an existing local checkout instead of cloning
uv run contractbench \
  --contractbench-root /path/to/contractbench \
  --output-dir ../../datasets/contractbench
```

**Parameters**:
- `--output-dir`: Output directory for generated tasks (default: `datasets/contractbench`).
- `--contractbench-root`: Use an existing checkout instead of cloning.
- `--ref`: Git ref to check out when cloning (default: pinned ref).
- `--task-ids`: Explicit source task names to convert (default: all 33); unknown names are an error.
- `--limit`: Generate only the first N tasks.
- `--overwrite`: Regenerate tasks whose output directory already exists.

The command exits non-zero if any selected task fails to generate.

## Comparison with Original Benchmark (Parity)

The oracle check is reported separately from agent parity: the oracle agent
scores **33/33 at reward 1.0** on the generated tasks (archived under `oracle/`
in the parity upload). The table is agent parity only.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|----------------|--------------|-----------------------|---------------------|
| contractbench-parity-agent@1.0.0 | gpt-4o-2024-08-06 | Mean reward (%) | 3 | 33 tasks (100% of full set) | 37.12 ± 2.31 | 36.06 ± 1.06 |
| contractbench-parity-agent@1.0.0 | gpt-4o-2024-08-06 | Pass rate, reward == 1.0 (%) | 3 | 33 tasks (100% of full set) | 32.32 ± 2.02 | 31.31 ± 2.02 |

Both metrics satisfy the matching criterion `max(A) >= min(B)` and
`max(B) >= min(A)`. Per-run scores are in `parity_experiment.json`; per-task
rewards for every run are in `results_collection/` of the parity upload.

**Setup.** Both sides run the same agent — ContractBench's tool-calling loop,
natively upstream and as `contractbench-parity-agent@1.0.0` in Harbor (see
[Adapter Features](#adapter-features)) — with `gpt-4o-2024-08-06` at
temperature 0.0, 30 model calls, a soft 600 s deadline (the original side ran
`run_task_docker.py --timeout 600`) and a 60 s per-command limit. The harness is
the only variable.

The three Harbor trials of `retry-backoff-compliance` first ran with the
upstream default 300 s deadline, before the original side's `--timeout 600` was
found; they reached it and scored 0. They were rerun at 600 s and again scored
0, now ending at the 30-call cap like the original side. No other Harbor trial
reached 300 s, so the deadline did not affect it. Both versions are in the
parity upload (`harbor_parity/run1` and `runs23`, then `harbor_parity/retry600`).

**Per-task comparison.** All 99 trials were scored on each side. No task is
solved on only one side. 28 of 33 tasks have the same number of passing runs
(out of 3) on both sides; the other 5 differ by one run, with the difference
going both ways:

| Task | Original rewards | Harbor rewards |
|------|------------------|----------------|
| `api-rate-limit-patience` | 0, 1, 0 | 1, 0, 0 |
| `multi-service-saga` | 0, 1, 0 | 1, 1, 0 |
| `multi-turn-recall` | 1, 1, 0 | 0, 0, 1 |
| `scheduled-maintenance` | 1, 1, 1 | 0, 1, 1 |
| `session-cookie-chain` | 0.5, 0.5, 1 | 1, 1, 0.5 |

Two more tasks changed one partial-credit value without changing the pass count
(`cascading-token-revocation` 1, 1, 1 vs 1, 1, 0.7; `oauth-pkce-with-rotation`
0.25 ×3 vs 0.25, 0.25, 0). Reading the traces of the largest single gap
(`scheduled-maintenance` run 1), both sides issue the same command sequence and
then miscount the instruction's seed-anchor characters; the original run
recovered by slicing the string in bash and the Harbor run did not. That is
sampling variance in the model, not an adaptation difference: the instruction
text is identical on both sides apart from the stripped footer.

**Standard Harbor agent.** To show the adapter is not tied to the parity agent,
`mini-swe-agent@2.4.6` (`gpt-4o-2024-08-06`, temperature 0.0, `step_limit` 30)
ran once on the same tasks: mean reward 34.22%, pass rate 28.12% over 32 scored
tasks (one trial lost to a container-setup network error). With Harbor 0.25.0
its install currently needs `--ae UV_EXCLUDE_NEWER=2026-09-15T00:00:00Z`: newer
litellm releases import `mcp`, which the install does not include.

**Links.**
- Original benchmark: https://github.com/SecurityLab-UCD/contractbench
- Original-side harness revision used for parity:
  https://github.com/SecurityLab-UCD/ContractBench/tree/e8b94270cdc8cf366820a276a3f4aac77aea2578
- Adapter PRs: https://github.com/harbor-framework/adapters/pull/8 (migrated from https://github.com/harbor-framework/harbor/pull/1949)
- Dataset PR: https://github.com/harbor-framework/harbor-datasets/pull/252
- Parity upload: https://huggingface.co/datasets/harborframework/parity-experiments/discussions/268

### Reproduction Steps

**Original benchmark side** (3 runs over all 33 tasks):

```bash
git clone https://github.com/SecurityLab-UCD/ContractBench && cd ContractBench
git checkout e8b94270cdc8cf366820a276a3f4aac77aea2578
uv sync --extra all
export OPENAI_API_KEY=...
uv run python experiments/scripts/run_task_docker.py --docker \
  --agent openai/gpt-4o-2024-08-06 --k 3 --temperature 0.0 --timeout 600 \
  --outdir results/parity
```

**Harbor side** (from the repository root; `n_attempts: 3` in the config
produces the three runs, so run the command once):

```bash
(cd src/contractbench && uv run contractbench --output-dir ../../datasets/contractbench)
export OPENAI_API_KEY=...
uv run --project src/contractbench --extra parity \
  harbor run -c src/contractbench/contractbench-parity.yaml
```

## Notes & Caveats

- **Model identifier.** The original-side runs requested `openai/gpt-4o`, and
  the upstream harness does not record the resolved model. Harbor-side
  trajectories collected the same day (2026-09-02) with the same API key record
  `gpt-4o-2024-08-06` as the model that served `gpt-4o`. The Harbor parity runs
  request `gpt-4o-2024-08-06` explicitly, and every response reports that model.
- **Instruction footer.** The upstream harness sends `instruction.md` with its
  provenance footer; the Harbor tasks strip it. This is the only difference in
  what the two sides send the model.
- **Benchmark-name scrub.** See Adapter Features (2). The signing secrets are
  hex-encoded rather than removed, so an agent that decodes them can still
  recover the name; removing them would require re-signing the precomputed
  instructions upstream.
- **Known limitations of the upstream tasks** (not changed by the adapter, since
  doing so would change the benchmark):
  - The task server runs in the same container as the agent, so its source,
    including expected payloads, is readable by the agent. In 10 of 33 tasks the
    expected answer appears literally in `server.py`.
  - The server's request log (`/root/logs/server.json`), which some verifiers
    read, is writable by the agent.
  - In some tasks the reward checks only the output file, not server-side
    evidence that the protocol was followed (e.g. `basic-oauth-token` exact-matches
    `/root/output/data.json`; `test_no_contract_failures` does not affect the
    reward).

  These are tracked for an upstream fix (running the server as a separate
  service).
- **Grader tampering.** Before this adapter's change (3), an agent could
  overwrite `/app/shared/server_logging.py` and receive reward 1.0 without
  solving the task. With the trusted copy in `tests/`, the same tampering
  scores 0.0. The agent still runs as root in the container the verifier uses.
- **Timeouts**: agent 300 s, verifier 120 s (per upstream `task.toml`). Upstream
  the agent limit is a soft deadline checked between turns, and the original-side
  parity runs raised it to 600 s with `--timeout 600`. The parity agent enforces
  the same soft deadline (`agent_timeout_sec: 600` in the parity config), and
  Harbor's hard limit is set to 1200 s so it never cuts a trial short.
- **Network**: tasks declare `network_mode = "public"`; the agent talks to the
  in-container FastAPI server on `http://localhost:8080`.
- **Task config schema**: upstream tasks declare `schema_version = "1.3"`.
  Harbor 0.25 reads the legacy `version` key as the old name of
  `schema_version` and loads these tasks as-is.

## Installation / Prerequisites

- Docker installed and running.
- [uv](https://docs.astral.sh/uv/); Harbor is installed through the adapter's
  `parity` extra (`harbor==0.25.0`).
- Internet connection to clone https://github.com/SecurityLab-UCD/contractbench
- API keys for agents/models (export as environment variables) when not using
  the oracle agent. The parity agent needs `OPENAI_API_KEY` (and
  `OPENAI_BASE_URL` if you use a regional endpoint) in the launching shell.

## Troubleshooting

- **Oracle scores 0 / empty server responses**: ensure Docker is running and the
  task container can start its FastAPI server; the oracle waits for readiness
  before issuing requests.
- **Clone fails**: check network access to
  `https://github.com/SecurityLab-UCD/contractbench`, or pass
  `--contractbench-root` to use a local checkout.
- **`ModuleNotFoundError: contractbench` when running the parity config**: run
  Harbor through `uv run --project src/contractbench --extra parity` so the
  adapter package (and its parity agent) is importable.

## Citation

If you use the ContractBench adapter, please cite the original work:

```bibtex
@article{wang2026contractbench,
  title={ContractBench: Can LLM Agents Preserve Observation Contracts?},
  author={Wang, Jicheng and He, Yifeng and Wang, Zili and Xing, Hanwen and De, Arkaprava and Chen, Hao},
  journal={arXiv preprint arXiv:2605.17281},
  year={2026}
}
```

## Authors & Contributions

- **Adapter**: [@JeremyJC67](https://github.com/JeremyJC67) (Jicheng Wang),
  who is also a ContractBench author.
- **Benchmark**: Jicheng Wang, Yifeng He, Zili Wang, Hanwen Xing, Arkaprava De,
  Hao Chen (see [Citation](#citation)).
- Issues and improvements welcome via the adapters repository.
