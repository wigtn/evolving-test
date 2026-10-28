"""워커: 자산 묶음으로 DeepAgents 에이전트를 조립해 요청 하나를 실행한다. 상태를 갖지 않는다.

실행 루틴은 standard-v1을 단순화했다: 자산 로드 → (쓰기 앞 승인은 request_approval 도구) → 턴 상한 → 응답.
실행 기록(RunRecord)과 trace는 실행기가 직접 남긴다 (Phoenix에 의존하지 않는다).
"""
from __future__ import annotations

import json
import time
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.errors import GraphRecursionError

from . import registry, tracing
from .config import TODAY, chat_model, worker_kind
from .world import TOOL_FUNCS, Gateway, World

_WORLD = None


def world() -> World:
    global _WORLD
    if _WORLD is None:
        _WORLD = World()
    return _WORLD


def system_prompt(bundle) -> str:
    parts = [f"오늘 날짜: {TODAY.isoformat()}"]
    parts += [p.body.strip() for p in bundle["prompts"]]
    for s in bundle["skills"]:
        parts.append(f"## SKILL {s.ref}\n{s.body.strip()}")
    parts.append("쓰기 작업의 HITL 승인은 request_approval 도구로 요청한다.")
    return "\n\n".join(parts)


def build_tools(bundle, gw: Gateway):
    tools = []
    for t in bundle["tools"]:
        fname, method = TOOL_FUNCS[t.base]
        fn = getattr(gw, method)

        schema = t.spec.get("input_schema", {"type": "object"})

        def call(_fn=fn, _schema=schema, _tool=t.base, **kw):
            # 모델이 필수 인자를 빠뜨리거나 정의에 없는 인자를 넣어도 실행 전체를 죽이지 않는다.
            # Gateway가 ARG_INVALID로 돌려주고 감사 기록에 남긴다 (R2의 원천).
            missing = [k for k in _schema.get("required", []) if k not in kw]
            extra = [k for k in kw if k not in _schema.get("properties", {})]
            if missing or extra:
                gw._log(_tool, kw, "error", "ARG_INVALID", detail={"missing": missing, "unexpected": extra})
                return json.dumps({"error": "ARG_INVALID", "missing": missing, "unexpected": extra}, ensure_ascii=False)
            try:
                return json.dumps(_fn(**kw), ensure_ascii=False)
            except (TypeError, ValueError) as e:
                gw._log(_tool, kw, "error", "ARG_INVALID", detail=str(e)[:200])
                return json.dumps({"error": "ARG_INVALID", "message": str(e)[:200]}, ensure_ascii=False)

        tools.append(StructuredTool.from_function(func=call, name=fname, description=t.spec.get("description", ""),
                                                  args_schema=t.spec.get("input_schema", {"type": "object"})))
    tools.append(StructuredTool.from_function(
        func=lambda summary: json.dumps(gw.request_approval(summary), ensure_ascii=False),
        name="request_approval", description="쓰기 작업 전 HITL 승인을 요청한다. 승인되면 approval_token을 돌려준다.",
        args_schema={"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}))
    return tools


def _model():
    m = chat_model("agent")
    if m is None:
        from .fake_llm import RuleFollowingFakeModel
        m = RuleFollowingFakeModel()
    return m


def run(user_message: str, *, user_id: str, source: str, overrides: dict | None = None,
        simulator=None, case_id: str | None = None, arm: str | None = None, repeat: int = 0,
        faults: dict | None = None, seed: int = 0, batch: str | None = None) -> dict:
    import random

    bundle = registry.resolve_agent(overrides=overrides)
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    gw = Gateway(world(), run_id, account="eval" if source == "eval" else "prod",
                 faults=faults if faults is not None else registry.read_state().get("faults", {}),
                 rng=random.Random(seed))
    max_turns = int((bundle["agent"].spec.get("params") or {}).get("max_turns", 12))
    from langchain.agents.middleware import ModelCallLimitMiddleware
    # max_turns = 사용자 턴 하나에서 허용하는 모델 호출 수. 넘으면 그때까지의 기록을 남기고 끝낸다 (R8 budget_exceeded)
    mw = [ModelCallLimitMiddleware(run_limit=max_turns, exit_behavior="end")]
    if worker_kind() == "deepagents":
        from deepagents import create_deep_agent
        agent = create_deep_agent(model=_model(), tools=build_tools(bundle, gw), system_prompt=system_prompt(bundle), middleware=mw)
    else:
        from langchain.agents import create_agent
        agent = create_agent(model=_model(), tools=build_tools(bundle, gw), system_prompt=system_prompt(bundle), middleware=mw)

    t0 = time.time()
    messages = [HumanMessage(user_message)]
    status, followups = "ok", []
    for _ in range(3):  # 사용자 턴 최대 3
        try:
            out = agent.invoke({"messages": messages}, config={"recursion_limit": max_turns * 6 + 20})
            messages = out["messages"]
        except GraphRecursionError:
            status = "budget_exceeded"
            break
        except Exception as e:  # 런타임 장애는 자산 결과가 아니다 (G4)
            status, followups = "runtime_error", followups + [repr(e)[:300]]
            break
        last = messages[-1]
        if isinstance(last, ToolMessage) or (isinstance(last, AIMessage) and (last.tool_calls or "Model call limits exceeded" in str(last.content))):
            status = "budget_exceeded"
            break
        reply = simulator.follow_up(last.content if isinstance(last, AIMessage) else "") if simulator else None
        if not reply:
            break
        followups.append(reply)
        messages = messages + [HumanMessage(reply)]

    calls = []
    for m in messages:
        if isinstance(m, AIMessage):
            for c in m.tool_calls or []:
                calls.append({"id": c["id"], "name": c["name"], "args": c["args"]})
    res = {m.tool_call_id: m.content for m in messages if isinstance(m, ToolMessage)}
    for c in calls:
        try:
            c["result"] = json.loads(res.get(c["id"], "null"))
        except Exception:
            c["result"] = res.get(c["id"])
    finals = [m.content for m in messages if isinstance(m, AIMessage) and not m.tool_calls and isinstance(m.content, str)]
    rec = {
        "run_id": run_id, "source": source, "batch": batch, "case_id": case_id, "arm": arm, "repeat": repeat,
        "user_id": user_id, "user_message": user_message, "followups": followups,
        "asset_refs": bundle["refs"], "status": status,
        "turns": sum(isinstance(m, AIMessage) for m in messages), "max_turns": max_turns,
        "tool_calls": calls, "answers": finals, "final_answer": finals[-1] if finals else "",
        "audit": gw.audit, "latency_s": round(time.time() - t0, 3),
    }
    tracing.record_run(rec)
    return rec
