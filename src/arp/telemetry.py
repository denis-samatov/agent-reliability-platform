"""OpenTelemetry tracing for runs, steps, tool calls and LLM calls.

* All spans of one run share a trace id derived from the run id, so a run that pauses for
  approval and resumes in another process still forms one trace.
* LLM spans follow the OpenTelemetry GenAI semantic conventions for attribute names
  (``gen_ai.operation.name``, ``gen_ai.request.model``, ``gen_ai.usage.input_tokens``,
  ``gen_ai.usage.output_tokens``).
* Spans are exported as JSON lines (one file per run) for the dashboard; an OTLP exporter
  can be added without code changes elsewhere. Prompt/tool payloads are *not* recorded,
  only sizes and redacted error messages.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags


class RunIdGenerator(IdGenerator):
    """Random span ids; trace ids are inherited from the run's synthetic parent context."""

    def __init__(self):
        self._rand = __import__("random").SystemRandom()

    def generate_span_id(self) -> int:
        return self._rand.getrandbits(64)

    def generate_trace_id(self) -> int:
        return self._rand.getrandbits(128)


class JsonlSpanExporter(SpanExporter):
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self.lock:
            for s in spans:
                run_id = s.attributes.get("run.id") or s.resource.attributes.get("run.id") or "unknown"
                rec = {
                    "trace_id": f"{s.context.trace_id:032x}",
                    "span_id": f"{s.context.span_id:016x}",
                    "parent_id": f"{s.parent.span_id:016x}" if s.parent else None,
                    "name": s.name,
                    "start_ns": s.start_time,
                    "end_ns": s.end_time,
                    "status": s.status.status_code.name,
                    "attributes": dict(s.attributes),
                }
                with (self.dir / f"{run_id}.jsonl").open("a") as fh:
                    fh.write(json.dumps(rec, default=str) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def trace_id_for(run_id: str) -> int:
    return uuid.UUID(run_id).int


class Tracer:
    def __init__(self, provider: TracerProvider, run_id: str):
        self.tracer = provider.get_tracer("arp")
        self.run_id = run_id
        self._root = None

    @contextmanager
    def run_span(self, run, workflow):
        # Parent all spans of this run under a synthetic remote context with the run's trace id.
        parent = trace.set_span_in_context(
            NonRecordingSpan(
                SpanContext(
                    trace_id=trace_id_for(run.id),
                    span_id=uuid.UUID(run.id).int & ((1 << 64) - 1) or 1,
                    is_remote=True,
                    trace_flags=TraceFlags(TraceFlags.SAMPLED),
                )
            )
        )
        with self.tracer.start_as_current_span(
            "workflow.run",
            context=parent,
            attributes={
                "run.id": run.id,
                "tenant": run.tenant,
                "workflow.name": workflow.name,
                "workflow.version": workflow.version,
                "workflow.version_hash": run.version_hash,
            },
        ) as span:
            yield span

    @contextmanager
    def step_span(self, name: str, attempt: int):
        with self.tracer.start_as_current_span(
            f"workflow.step {name}", attributes={"run.id": self.run_id, "step.name": name, "step.attempt": attempt}
        ) as span:
            try:
                yield span
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, type(e).__name__))
                span.set_attribute("error.type", type(e).__name__)
                raise

    @contextmanager
    def tool_span(self, tool: str, side_effect: bool):
        with self.tracer.start_as_current_span(
            f"tool.call {tool}", attributes={"run.id": self.run_id, "tool.name": tool, "tool.side_effect": side_effect}
        ) as span:
            yield span

    @contextmanager
    def llm_span(self, model: str):
        with self.tracer.start_as_current_span(
            f"gen_ai.chat {model}",
            attributes={"run.id": self.run_id, "gen_ai.operation.name": "chat", "gen_ai.request.model": model},
        ) as span:
            yield span


def make_provider(trace_dir: Path | None = None, extra_exporter: SpanExporter | None = None) -> TracerProvider:
    provider = TracerProvider(id_generator=RunIdGenerator())
    if trace_dir is not None:
        provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(trace_dir)))
    if extra_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(extra_exporter))
    return provider


def load_trace(trace_dir: Path, run_id: str) -> list[dict]:
    p = trace_dir / f"{run_id}.jsonl"
    if not p.exists():
        return []
    return sorted((json.loads(line) for line in p.read_text().splitlines() if line.strip()), key=lambda s: s["start_ns"])
