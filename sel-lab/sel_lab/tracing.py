"""실행 기록 저장. 항상 runs/traces/에 JSONL로 남기고, SEL_OTLP_ENDPOINT가 있으면 Phoenix로도 보낸다.

span 속성은 플랫폼 계약 이름을 따른다: erp.run_id, erp.asset.refs, erp.run.source, erp.eval.case_id, erp.eval.arm.
"""
from __future__ import annotations

import json
import time

from .config import RUNS, env

_otel = None


def _tracer():
    global _otel
    if _otel is not None or not env("SEL_OTLP_ENDPOINT"):
        return _otel or None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        tp = TracerProvider()
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=env("SEL_OTLP_ENDPOINT"))))
        trace.set_tracer_provider(tp)
        _otel = trace.get_tracer("sel-lab")
    except ImportError:
        _otel = False
    return _otel or None


def _append(name: str, row: dict) -> None:
    p = RUNS / "traces" / f"{name}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def record_run(rec: dict) -> None:
    _append("runs_eval" if rec["source"] == "eval" else "runs_prod", rec)
    for a in rec["audit"]:
        _append("audit", {**a, "source": rec["source"], "ts": time.time()})
    t = _tracer()
    if not t:
        return
    with t.start_as_current_span("agent.run") as s:
        s.set_attribute("openinference.span.kind", "AGENT")
        s.set_attribute("erp.run_id", rec["run_id"])
        s.set_attribute("erp.run.source", rec["source"])
        s.set_attribute("erp.asset.refs", rec["asset_refs"])
        s.set_attribute("input.value", rec["user_message"])
        s.set_attribute("output.value", rec["final_answer"])
        if rec.get("case_id"):
            s.set_attribute("erp.eval.case_id", rec["case_id"])
            s.set_attribute("erp.eval.arm", rec.get("arm") or "")
        for c in rec["tool_calls"]:
            with t.start_as_current_span(f"tool.{c['name']}") as ts:
                ts.set_attribute("openinference.span.kind", "TOOL")
                ts.set_attribute("tool.name", c["name"])
                ts.set_attribute("input.value", json.dumps(c["args"], ensure_ascii=False))
                r = c.get("result") or {}
                err = r.get("error") if isinstance(r, dict) else None
                ts.set_attribute("erp.tool.result_class", "error" if err else "ok")
                if err:
                    ts.set_attribute("erp.tool.error", err)


def record_session(row: dict) -> None:
    _append("sessions", row)


def read(name: str) -> list[dict]:
    p = RUNS / "traces" / f"{name}.jsonl"
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]
