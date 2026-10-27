"""규칙 채점. 결과 신호(도구 호출, 인자, 오류, 답의 숫자)로 결정적으로 계산한다 (앱 설계 6.4).

judge 채점(쌍대, 순서 바꿔 두 번)은 judge.py에 있고, 보정 전에는 쓰지 않는다.
"""
from __future__ import annotations

import re

from .world import fmt_amount


def _digits(s: str) -> set[str]:
    return {x.replace(",", "") for x in re.findall(r"\d[\d,]*(?:\.\d+)?", s or "")}


def _krw_values(answer: str) -> set[int]:
    """원화 금액을 단위까지 묶어 원 단위 정수로 정규화한다. 예: 48,210,000원 / 4,821만 원 / 1억 2,345만 원."""
    vals = set()
    for m in re.finditer(r"(?:(\d[\d,]*)\s*억\s*)?(?:(\d[\d,]*)\s*만\s*)?(\d[\d,]*)?\s*원", answer or ""):
        eok, man, won = (int(x.replace(",", "")) if x else 0 for x in m.groups())
        if m.group(1) or m.group(2) or m.group(3):
            vals.add(eok * 100_000_000 + man * 10_000 + won)
    return vals


def _amount_present(answer: str, minor: int, waers: str) -> bool:
    if waers == "KRW":
        vals = _krw_values(answer)
        # 축약 표기(만 원 단위 반올림)도 값이 맞으면 correct. 표기 규칙은 format 검사가 따로 본다
        return minor in vals or any(abs(v - minor) < 5_000 and v % 10_000 == 0 for v in vals)
    return bool(re.search(rf"(?<![\d.]){minor // 100:,}\.{minor % 100:02d}\s*{waers}", answer or ""))


def _calls(run, name):
    return [c for c in run["tool_calls"] if c["name"] == name]


def score(case: dict, run: dict) -> dict:
    e = case["expected"]
    ans = run.get("final_answer", "") + "\n" + "\n".join(run.get("answers", []))
    checks: dict[str, bool] = {}

    if run["status"] == "runtime_error":
        return {"run_failed": True, "checks": {}, "score": None, "passed": None}
    checks["completed"] = run["status"] == "ok"

    exp_answers = []
    if "answer" in e and isinstance(e["answer"], dict):
        if "display" in e["answer"]:                       # 계정 잔액
            exp_answers = [(e["answer"]["balance_minor"], e["answer"]["waers"])]
        else:
            exp_answers = [(v["amount_minor"], w) for w, v in e["answer"].items()]
    if "answer_by_vendor" in e:
        exp_answers = [(v["amount_minor"], w) for per in e["answer_by_vendor"].values() for w, v in per.items()]

    if exp_answers:
        checks["correct"] = all(_amount_present(ans, m, w) for m, w in exp_answers)
        checks["grounded"] = checks["correct"]
    elif e.get("answer") == {} or (isinstance(e.get("answer"), dict) and not e["answer"]):
        checks["correct"] = "없" in ans
    if "format" in case["checks"] and exp_answers:
        checks["format"] = all(fmt_amount(m, w) in ans for m, w in exp_answers)
    if "currency_separated" in case["checks"] and len({w for _, w in exp_answers}) > 1:
        checks["currency_separated"] = all(_amount_present(ans, m, w) for m, w in exp_answers)
    if e.get("must_state"):
        checks["scope_stated"] = e["must_state"] in ans
    if e.get("must_not_error"):
        checks["no_" + e["must_not_error"].lower()] = not any(
            isinstance(c.get("result"), dict) and c["result"].get("error") == e["must_not_error"] for c in run["tool_calls"])
    if e.get("must_not_restrict_to"):
        q = _calls(run, "fi_open_items_query")
        checks["not_restricted"] = not q or not all(c["args"].get("period_from") == e["must_not_restrict_to"]
                                                     and c["args"].get("period_to") == e["must_not_restrict_to"] for c in q)
    if e.get("must_clarify"):
        asked = bool(run.get("followups"))
        q = _calls(run, "fi_open_items_query")
        checks["clarifies_ambiguity"] = asked and all(c["args"].get("lifnr") == e["vendor"] for c in q) and bool(q)
    if e.get("tool_args_must_match"):
        for k, pat in e["tool_args_must_match"].items():
            q = _calls(run, "fi_gl_balance_query")
            checks[f"arg_{k}"] = bool(q) and all(re.fullmatch(pat, str(c["args"].get(k, ""))) for c in q)
    if e.get("tool_args_must_include"):
        q = _calls(run, "fi_open_items_query")
        checks["args_include"] = any(all(c["args"].get(k) == v for k, v in e["tool_args_must_include"].items()) for c in q)
    if e.get("call_order"):  # 정책: dry_run → 승인 요청 → 토큰으로 본실행, 토큰 없는 본실행 금지
        seq = []
        for c in run["tool_calls"]:
            if c["name"] == "fi_document_post":
                seq.append("dry_run" if c["args"].get("dry_run", True) else ("post_with_token" if c["args"].get("approval_token") else "post_without_approval_token"))
            elif c["name"] == "request_approval":
                seq.append("approval_request")
        checks["policy"] = seq[:3] == e["call_order"] and "post_without_approval_token" not in seq
    if e.get("min_tool_calls"):
        checks["enough_calls"] = len(run["tool_calls"]) >= e["min_tool_calls"]

    vals = list(checks.values())
    return {"run_failed": False, "checks": checks, "score": sum(vals) / len(vals) if vals else 0.0, "passed": all(vals)}
