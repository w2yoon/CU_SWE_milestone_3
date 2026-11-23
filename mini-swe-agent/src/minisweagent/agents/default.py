"""Basic agent class. See https://mini-swe-agent.com/latest/advanced/control_flow/ for visual explanation
or https://minimal-agent.com for a tutorial on the basic building principles.
"""

import json
import logging
import time
import traceback
from pathlib import Path

from jinja2 import StrictUndefined, Template
from pydantic import BaseModel

from minisweagent import Environment, Model, __version__
from minisweagent.exceptions import FormatError, InterruptAgentFlow, LimitsExceeded, TimeExceeded
from minisweagent.utils.serialize import recursive_merge


class AgentConfig(BaseModel):
    """Check the config files in minisweagent/config for example settings."""

    system_template: str
    """Template for the system message (the first message)."""
    instance_template: str
    """Template for the first user message specifying the task (the second message overall)."""
    step_limit: int = 0
    """Maximum number of steps the agent can take."""
    cost_limit: float = 3.0
    """Stop agent after exceeding (!) this cost."""
    wall_time_limit_seconds: int = 0
    """Stop agent after this many seconds of wall-clock time. 0 means no limit."""
    max_consecutive_format_errors: int = 25
    """Exit after this many format errors in a row (0 = no limit). Set high (25) so weak
    models (e.g. GPT 5.6 Luna) that repeatedly batch multiple command blocks get many
    chances to recover to a single valid action before the run is aborted."""
    output_path: Path | None = None
    """Save the trajectory to this path."""
    format_error_log_path: Path | None = None
    """Append each rejected (FormatError) model response here as JSONL, for bad-example
    mining and manual investigation. If None, defaults to <output_path dir>/format_errors.jsonl."""
    api_coverage_tool: bool = False
    """Enable the `api-coverage` command (minisweagent/tools/api_coverage.py) for this run."""
    memory: bool = False
    """Working memory: show the notes file and a step/time/cost meter after every command,
    and trim old steps from the prompt (the saved trajectory keeps everything)."""
    memory_notes_path: str = "/app/NOTES.md"
    memory_notes_max_chars: int = 3000
    memory_keep_recent_steps: int = 8
    """The newest steps are always sent in full."""
    memory_trim_every_steps: int = 10
    """Older steps are trimmed in blocks of this many steps, so the prompt prefix stays the same
    between blocks (keeps the provider's prompt cache useful)."""
    memory_trim_chars: int = 600
    """A trimmed message keeps this many characters (half from the start, half from the end)."""
    memory_time_budget_seconds: int = 3000
    memory_hold_early_submit: float = 0.6
    """With memory on, a submit is held back while both time and cost used are below this fraction..."""
    memory_max_holds: int = 3
    """...up to this many times per run."""


class DefaultAgent:
    def __init__(self, model: Model, env: Environment, *, config_class: type = AgentConfig, **kwargs):
        """See the `AgentConfig` class for permitted keyword arguments."""
        self.config = config_class(**kwargs)
        self.messages: list[dict] = []
        self.model = model
        self.env = env
        self.extra_template_vars = {}
        self.logger = logging.getLogger("agent")
        self.cost = 0.0
        self.n_calls = 0
        self.n_consecutive_format_errors = 0
        self._start_time = time.time()
        if self.config.api_coverage_tool:
            env_vars = getattr(getattr(self.env, "config", None), "env", None)
            if isinstance(env_vars, dict):
                env_vars |= {"MSWEA_API_COVERAGE": "1", "MSWEA_SILENT_STARTUP": "1"}

    def get_template_vars(self, **kwargs) -> dict:
        return recursive_merge(
            self.config.model_dump(),
            self.env.get_template_vars(),
            self.model.get_template_vars(),
            {
                "n_model_calls": self.n_calls,
                "model_cost": self.cost,
                "elapsed_seconds": int(time.time() - self._start_time),
            },
            self.extra_template_vars,
            kwargs,
        )

    def _render_template(self, template: str) -> str:
        return Template(template, undefined=StrictUndefined).render(**self.get_template_vars())

    def add_messages(self, *messages: dict) -> list[dict]:
        self.logger.debug(messages)  # set log level to debug to see
        self.messages.extend(messages)
        return list(messages)

    def handle_uncaught_exception(self, e: Exception) -> list[dict]:
        return self.add_messages(
            self.model.format_message(
                role="exit",
                content=str(e),
                extra={
                    "exit_status": type(e).__name__,
                    "submission": "",
                    "exception_str": str(e),
                    "traceback": traceback.format_exc(),
                },
            )
        )

    def run(self, task: str = "", **kwargs) -> dict:
        """Run step() until agent is finished. Returns dictionary with exit_status, submission keys."""
        self.extra_template_vars |= {"task": task, **kwargs}
        self.messages = []
        self.add_messages(
            self.model.format_message(role="system", content=self._render_template(self.config.system_template)),
            self.model.format_message(role="user", content=self._render_template(self.config.instance_template)),
        )
        while True:
            try:
                self.step()
                self.n_consecutive_format_errors = 0  # reset on any clean step
            except FormatError as e:
                # The call was billed before parsing failed, so query() never got to charge it.
                self.cost += e.messages[0].get("extra", {}).get("cost", 0.0)
                self._log_format_error(e)  # persist the raw bad request for later inspection
                self.n_consecutive_format_errors += 1
                if 0 < self.config.max_consecutive_format_errors <= self.n_consecutive_format_errors:
                    self.add_messages(
                        *e.messages,
                        {
                            "role": "exit",
                            "content": "RepeatedFormatError",
                            "extra": {"exit_status": "RepeatedFormatError", "submission": ""},
                        },
                    )
                else:
                    self.add_messages(*e.messages)
            except InterruptAgentFlow as e:
                self.add_messages(*e.messages)
            except Exception as e:
                self.handle_uncaught_exception(e)
                raise
            finally:
                self.save(self.config.output_path)
            if self.messages[-1].get("role") == "exit":
                break
        return self.messages[-1].get("extra", {})

    def _log_format_error(self, e: FormatError) -> None:
        # AIDEV-NOTE: persist each rejected model response (raw "bad request") for
        # bad-example mining and manual investigation -- it is otherwise dropped from
        # the saved trajectory. One JSON object per line: timestamp, step, n_actions, response.
        path = self.config.format_error_log_path
        if path is None:
            if self.config.output_path is None:
                return
            path = Path(self.config.output_path).parent / "format_errors.jsonl"
        extra = e.messages[0].get("extra", {}) if e.messages else {}
        record = {
            "timestamp": time.time(),
            "n_calls": self.n_calls,
            "n_actions": extra.get("n_actions"),
            "model_response": extra.get("model_response", ""),
        }
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            # Logging a bad request must never abort the run.
            self.logger.warning("Could not write format-error log to %s", path)

    def step(self) -> list[dict]:
        """Query the LM, execute actions."""
        return self.execute_actions(self.query())

    def query(self) -> dict:
        """Query the model and return model messages. Override to add hooks."""
        if 0 < self.config.step_limit <= self.n_calls or 0 < self.config.cost_limit <= self.cost:
            raise LimitsExceeded(
                {
                    "role": "exit",
                    "content": "LimitsExceeded",
                    "extra": {"exit_status": "LimitsExceeded", "submission": ""},
                }
            )
        if 0 < self.config.wall_time_limit_seconds <= int(time.time() - self._start_time):
            raise TimeExceeded(
                {
                    "role": "exit",
                    "content": "TimeExceeded",
                    "extra": {"exit_status": "TimeExceeded", "submission": ""},
                }
            )
        self.n_calls += 1
        message = self.model.query(self.prompt_messages() if self.config.memory else self.messages)
        self.cost += message.get("extra", {}).get("cost", 0.0)
        self.add_messages(message)
        return message

    def prompt_messages(self) -> list[dict]:
        """The messages sent to the model when memory is on: old steps trimmed, memory block appended."""
        head, body = self.messages[:2], self.messages[2:]
        cfg = self.config
        n_steps = sum(m.get("role") == "assistant" for m in body)
        n_trim = max(0, n_steps - cfg.memory_keep_recent_steps) // cfg.memory_trim_every_steps * cfg.memory_trim_every_steps
        out, seen = [], 0
        for msg in body:
            seen += msg.get("role") == "assistant"
            content = msg.get("content")
            if seen <= n_trim and isinstance(content, str) and len(content) > cfg.memory_trim_chars:
                half = cfg.memory_trim_chars // 2
                content = (f"{content[:half]}\n[... {len(content) - 2 * half} chars of this old step were trimmed from your "
                           f"context. Re-read files or re-run commands if you need them; keep what matters in "
                           f"{cfg.memory_notes_path} ...]\n{content[-half:]}")
                msg = msg | {"content": content}
            out.append(msg)
        if out and isinstance(out[-1].get("content"), str):
            block = self.memory_block()
            out[-1] = out[-1] | {"content": out[-1]["content"] + "\n" + block}
            self.messages[-1].setdefault("extra", {})["memory_block"] = block
            print(f"[memory] {block.splitlines()[1]} | notes {len(block)} chars | {n_trim} old steps trimmed", flush=True)
        return head + out

    def hold_early_submit(self, message: dict) -> list[dict] | None:
        """With memory on, hold back the first submit that comes early; returns the observation messages if held."""
        if not (self.config.memory and any(self._is_early_submit(a) for a in message.get("extra", {}).get("actions", []))):
            return None
        self._submit_holds = getattr(self, "_submit_holds", 0) + 1
        outputs = [{"output": self._hold_message(), "returncode": 1, "exception_info": ""}]
        return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))

    def _is_early_submit(self, action: dict) -> bool:
        cfg = self.config
        frac = cfg.memory_hold_early_submit
        return (
            "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in action.get("command", "")
            and getattr(self, "_submit_holds", 0) < cfg.memory_max_holds
            and time.time() - self._start_time < frac * cfg.memory_time_budget_seconds
            and (not cfg.cost_limit or self.cost < frac * cfg.cost_limit)
        )

    def _hold_message(self) -> str:
        minutes = int(time.time() - self._start_time) // 60
        return (
            f"Submission held back ({getattr(self, '_submit_holds', 0)} of {self.config.memory_max_holds}) by the memory system: you have used only {minutes} of "
            f"{self.config.memory_time_budget_seconds // 60} minutes and ${self.cost:.2f} of ${self.config.cost_limit:.2f}. "
            f"Nothing was submitted. Before submitting, go through the task's contract section by section, compare it with "
            f"{self.config.memory_notes_path}, and write down which rules you have not yet tested with real requests "
            f"(permissions, validation errors, pagination, mentions, read state, WebSocket resume, IRC bridge, restarts, UI flows). "
            f"Test and fix those, including the browser UI flows of the task (sign-up, channels, messages, threads, reactions). "
            f"A submit is held back up to {self.config.memory_max_holds} times while less than "
            f"{int(self.config.memory_hold_early_submit * 100)}% of both the time and the budget is used."
        )

    def memory_block(self) -> str:
        cfg = self.config
        path = Path(cfg.memory_notes_path)
        notes = path.read_text(errors="replace").strip() if path.is_file() else ""
        if not notes:
            notes = f"(empty: {path} does not exist yet. Create it now with your plan.)"
        elif len(notes) > cfg.memory_notes_max_chars:
            notes = notes[: cfg.memory_notes_max_chars] + f"\n[... notes cut at {cfg.memory_notes_max_chars} chars: shorten {path}]"
        elapsed = int(time.time() - self._start_time) // 60
        cost = f"${self.cost:.2f} of ${cfg.cost_limit:.2f}" if cfg.cost_limit else f"${self.cost:.2f}"
        return (f"<memory>\nstep {self.n_calls} | {elapsed} of {cfg.memory_time_budget_seconds // 60} min used | cost {cost}\n"
                f"your notes ({path}):\n{notes}\n</memory>")

    def execute_actions(self, message: dict) -> list[dict]:
        """Execute actions in message, add observation messages, return them."""
        if held := self.hold_early_submit(message):
            return held
        outputs = [self.env.execute(action) for action in message.get("extra", {}).get("actions", [])]
        return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))

    def serialize(self, *extra_dicts) -> dict:
        """Serialize agent state to a json-compatible nested dictionary for saving."""
        last_message = self.messages[-1] if self.messages else {}
        last_extra = last_message.get("extra", {})
        agent_data = {
            "info": {
                "model_stats": {
                    "instance_cost": self.cost,
                    "api_calls": self.n_calls,
                },
                "config": {
                    "agent": self.config.model_dump(mode="json"),
                    "agent_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                },
                "mini_version": __version__,
                "exit_status": last_extra.get("exit_status", ""),
                "submission": last_extra.get("submission", ""),
            },
            "messages": self.messages,
            "trajectory_format": "mini-swe-agent-1.1",
        }
        return recursive_merge(agent_data, self.model.serialize(), self.env.serialize(), *extra_dicts)

    def save(self, path: Path | None, *extra_dicts) -> dict:
        """Save the trajectory of the agent to a file if path is given. Returns full serialized data.
        You can pass additional dictionaries with extra data to be (recursively) merged into the output data.
        """
        data = self.serialize(*extra_dicts)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2))
        return data
