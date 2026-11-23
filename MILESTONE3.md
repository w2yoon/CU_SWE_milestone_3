# Milestone 3: agent improvements

This PR adds two improvements to mini-swe-agent for the Slack-clone task: an `api-coverage` tool and
working memory. Both live in the agent, so `/spec` stays empty. This file documents what changed, how to
switch each improvement on or off, and what we saw in the first runs. It is not the final report.

## Switches

Both improvements are controlled by two flags in the `agent:` section of
`tasks/slack-clone-course/course-staff-files/final_task/agent_llm_config/mswea-openai-gpt-5.6-luna-no-reasoning.yaml`:

| Flag | Turns on |
| --- | --- |
| `api_coverage_tool` | the `api-coverage` command and its prompt section |
| `memory` | notes file, budget meter, context trimming, early-submit hold |

Both are `true` in this PR. With both `false`, the prompt is the original prompt, character for character,
and the agent behaves exactly as before. The code is the same in every setting.

## Improvement 1: `api-coverage` tool

A command the agent can run inside its container
(`mini-swe-agent/src/minisweagent/tools/api_coverage.py`). It is installed as a console script through
`mini-swe-agent/pyproject.toml`, and `DefaultAgent` enables it when the flag is on.

In under 30 seconds it probes the running cluster on `127.0.0.1`:

- **Routes (81 probes):** one request per route in the `instruction.md` contract. It also checks health on
  all three nodes, Redis, `/app/start.sh`, the WebSocket (bad-token rejection, subscribe, cross-node
  `message.created`), IRC registration and the pinned `data-testid` hooks in the HTML at `/`.
- **Behaviour rules (35 probes):** permissions, validation, pagination cursors, edits and deletes,
  reactions, mentions, read state, search, slash commands, file limits, dense `event_id` and resume, and the
  IRC bridge.
- **`--resilience`:** kills the process on `:8001` and `redis-server`, then checks that the other nodes keep
  serving and that `/app/start.sh` restarts both.

It prints one summary line, a score per area, and one line for each probe that is `DOWN`, `ERROR`,
`MISSING`, `WRONG` or `SKIPPED`. It is written from `instruction.md` only, not from the Milestone 2 checks.

**Problem it targets:** with an empty `/spec`, the agent has no way to see which parts of the contract it
has not built yet.

## Improvement 2: working memory

Changes to the agent loop in `mini-swe-agent/src/minisweagent/agents/default.py`, plus one line in
`interactive.py` so the hold also applies to the agent class the harness uses.

1. **Notes and meter.** The prompt asks the agent to keep `/app/NOTES.md`. After every command, the agent
   sees a `<memory>` block with the step number, minutes used of 50, cost so far of $3, and the current
   notes (up to 3,000 characters).
2. **Context trimming.** The newest 8 steps are kept in full. Older messages are cut to their first and last
   300 characters before they are sent to the model. The saved trajectory keeps everything.
3. **Early-submit hold.** A submit sent while less than 60% of both the time and the budget is used is held
   back, up to 3 times per run. The agent is asked to check its notes against the task first.

**Problem it targets:** in baseline runs the agent stopped after about 2 minutes and $0.09, with most of
its time and budget unused.

## First results

8 runs on GPT 5.6 Luna, 2 per setting. Each final app was re-checked in a fresh container against 244
rules (the Milestone 0 boot checks plus our Milestone 2 checks for components A to E). The job folders are
in `tasks/slack-clone-course/jobs/2026-11-22__*`.

| Setting | Run 1 | Run 2 | Mean | Mean cost |
| --- | --- | --- | --- | --- |
| Baseline | 54 | 70 | 62 | $0.09 |
| Tool only | 95 | 28 | 61.5 | $0.33 |
| Memory only | 145 | 156 | 150.5 | $1.81 |
| Both | 146 | 134 | 140 | $1.58 |

- Memory more than doubles the baseline in both runs. The hold stops the early "done", and trimming
  roughly halves the input tokens, which keeps the longer runs under $3.
- The tool alone is inconsistent. It finds the failures, but without memory the agent still submits early.
- Both together score close to memory alone.
- The browser UI flows fail in every setting.

Total cost of the 8 runs: $7.62.

## Files changed

- `mini-swe-agent/src/minisweagent/tools/__init__.py`, `tools/api_coverage.py` (new): the tool
- `mini-swe-agent/pyproject.toml`: `api-coverage` console script
- `mini-swe-agent/src/minisweagent/agents/default.py`: memory, and the switch for the tool
- `mini-swe-agent/src/minisweagent/agents/interactive.py`: applies the hold in the harness's agent class
- the luna yaml above: prompt sections and the two flags
- `milestone-3-specs/README.md` (new): says `/spec` holds no specs
- `spec-copies/` and `tasks/slack-clone-course/jobs/2026-11-22__*`: the 8 runs

## How to run

From the repo root, with the course setup done (Docker, `uv`, OpenAI key in `dev.env`):

```bash
uv sync
cd tasks/slack-clone-course/student-files
ln -sfn ../course-staff-files/final_task/environment/milestone-specs/milestone-3-specs spec
./run_student_openai.sh 3
```

If `No module named 'mini_swe_agent_local'` appears, run `uv sync --reinstall-package long-horizon` from the
repo root first. To try one improvement on its own, set the other flag to `false` in the yaml.
