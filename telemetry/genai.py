"""OpenTelemetry GenAI semantic conventions for every model call.

Two reasons this exists, and the second is the interesting one.

The first is ordinary: a system that spends money and latency on a model should
be able to say how much, per decision, and see it move. Tokens, duration,
finish reasons and tool calls are emitted under the OTel GenAI semantic
conventions rather than under names invented here, so Grafana's own LLM
observability surfaces read them without translation and so the numbers mean
the same thing they mean in every other instrumented system.

The second is the one that matters for trust. The agent's proposals are checked
by the contracts in agents/contracts.py -- no claim without re-runnable
evidence, no repair without a falsifiable prediction -- and those checks either
pass or they reject the model's output. **How often they reject is itself a
measurement.** `continuity_agent_rejections_total{reason}` turns the guardrails
from an assertion in a README into a time series a judge can watch: if the
counter is flat at zero the guardrails may be theatre, and if it moves you can
see exactly which invariant the model tried to walk past and how often.

Nothing here decides anything. It observes the deciding.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

# ---- GenAI semantic conventions -------------------------------------------
# Names from the OTel GenAI semconv rather than our own, so Grafana's LLM
# observability reads them unmodified.
ATTR_SYSTEM = "gen_ai.system"
ATTR_PROVIDER = "gen_ai.provider.name"
ATTR_OPERATION = "gen_ai.operation.name"
ATTR_REQUEST_MODEL = "gen_ai.request.model"
ATTR_RESPONSE_MODEL = "gen_ai.response.model"
ATTR_TEMPERATURE = "gen_ai.request.temperature"
ATTR_MAX_TOKENS = "gen_ai.request.max_tokens"
ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ATTR_FINISH_REASONS = "gen_ai.response.finish_reasons"
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_TOOL_CALL_ID = "gen_ai.tool.call.id"
ATTR_CONVERSATION_ID = "gen_ai.conversation.id"

SYSTEM = "gcp.gemini"

# ---- series ---------------------------------------------------------------
# Semconv metric names, flattened the way our registry requires (see
# telemetry/metrics.py on why every instrument declares unit="").
TOKEN_USAGE = "gen_ai_client_token_usage"
OPERATION_DURATION = "gen_ai_client_operation_duration_seconds"

AGENT_STEPS = "continuity_agent_steps_total"
TOOL_CALLS = "continuity_agent_tool_calls_total"
REJECTIONS = "continuity_agent_rejections_total"
DECISIONS = "continuity_agent_decisions_total"


class GenAI:
    """Instrumentation for one agent's model calls and tool use."""

    def __init__(self, tracer: trace.Tracer, instruments: Any, agent: str) -> None:
        self.tracer = tracer
        self.instruments = instruments
        self.agent = agent

    # -- model calls -------------------------------------------------------

    @contextmanager
    def call(
        self, model: str, *, operation: str = "generate_content",
        temperature: float | None = None, conversation_id: str = "",
        extra: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        """Wrap one model call.

        Span name follows the semconv shape `{operation} {model}`, which is
        what makes a trace readable next to any other instrumented LLM system
        rather than only next to this one.
        """
        attrs: dict[str, Any] = {
            ATTR_SYSTEM: SYSTEM,
            ATTR_PROVIDER: SYSTEM,
            ATTR_OPERATION: operation,
            ATTR_REQUEST_MODEL: model,
            "continuity.agent": self.agent,
        }
        if temperature is not None:
            attrs[ATTR_TEMPERATURE] = temperature
        if conversation_id:
            attrs[ATTR_CONVERSATION_ID] = conversation_id
        if extra:
            attrs.update(extra)

        started = time.monotonic()
        with self.tracer.start_as_current_span(
            f"{operation} {model}", attributes=attrs
        ) as span:
            try:
                yield span
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                raise
            finally:
                self.instruments.gauge(
                    OPERATION_DURATION, "Model call duration, seconds"
                ).set(
                    time.monotonic() - started,
                    {"agent": self.agent, "model": model, "operation": operation},
                )

    def record_usage(self, span: Span, response: Any, model: str) -> None:
        """Token counts from the response, onto the span and the histogram.

        Read defensively: usage metadata is the field SDKs most often rename,
        and losing a cost measurement is not worth failing a repair over.
        """
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return
        counts = {
            "input": getattr(usage, "prompt_token_count", None),
            "output": getattr(usage, "candidates_token_count", None),
        }
        if counts["input"] is not None:
            span.set_attribute(ATTR_INPUT_TOKENS, int(counts["input"]))
        if counts["output"] is not None:
            span.set_attribute(ATTR_OUTPUT_TOKENS, int(counts["output"]))

        gauge = self.instruments.gauge(
            TOKEN_USAGE, "Tokens consumed by a model call"
        )
        for kind, value in counts.items():
            if value is not None:
                gauge.set(float(value), {
                    "agent": self.agent, "model": model, "token_type": kind,
                })

        reasons = [
            str(getattr(c, "finish_reason", "")) for c in
            (getattr(response, "candidates", None) or [])
        ]
        if any(reasons):
            span.set_attribute(ATTR_FINISH_REASONS, [r for r in reasons if r])

    # -- tool use ----------------------------------------------------------

    @contextmanager
    def tool(self, name: str, call_id: str = "") -> Iterator[Span]:
        """Wrap one tool invocation the model asked for.

        Tool calls get their own spans because "the agent decided" and "the
        agent looked something up" are different events, and a trace that
        conflated them could not answer whether a conclusion was grounded.
        """
        attrs = {
            ATTR_OPERATION: "execute_tool",
            ATTR_TOOL_NAME: name,
            "continuity.agent": self.agent,
        }
        if call_id:
            attrs[ATTR_TOOL_CALL_ID] = call_id
        outcome = "ok"
        with self.tracer.start_as_current_span(
            f"execute_tool {name}", attributes=attrs
        ) as span:
            try:
                yield span
            except Exception as exc:
                outcome = "error"
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                raise
            finally:
                self.instruments.counter(
                    TOOL_CALLS, "Tool invocations by the agent"
                ).add(1, {"agent": self.agent, "tool": name, "outcome": outcome})

    # -- the loop ----------------------------------------------------------

    def step(self, name: str) -> None:
        self.instruments.counter(
            AGENT_STEPS, "Control-loop steps taken"
        ).add(1, {"agent": self.agent, "step": name})

    def decision(self, kind: str, outcome: str) -> None:
        self.instruments.counter(
            DECISIONS, "Decisions the agent reached"
        ).add(1, {"agent": self.agent, "kind": kind, "outcome": outcome})

    def rejected(self, reason: str, detail: str = "") -> None:
        """A model proposal the contracts refused.

        The measurement that makes the guardrails checkable rather than
        claimed. A counter flat at zero means either a well-behaved model or a
        guardrail that never fires, and only a time series can tell you which.
        """
        self.instruments.counter(
            REJECTIONS, "Model proposals rejected by the contracts"
        ).add(1, {"agent": self.agent, "reason": reason})
        span = trace.get_current_span()
        if span is not None:
            span.add_event("proposal_rejected", {
                "continuity.rejection.reason": reason,
                "continuity.rejection.detail": detail[:400],
            })
