"""① 입력 파이프라인 대역. 운영 실행 기록 · Gateway 감사 기록 · 세션 기록을 집계해 finding을 낸다.

실제 ①은 ① 담당의 sel-ingest다. 이 대역은 그 규칙을 따른다(LLD v0.1).
- 패턴은 기준 문서 11장 5종 이름을 쓴다: tool_arg_error · limit_exceeded · repeat_question
  (wrong_tool_selection · user_approval_rejected는 모의 세계에 신호가 없어 생략).
  eval_fail(정기 골든셋 회귀) · no_asset_match(맞는 자산 없음)는 lab 전용 패턴이다.
- 도구가 정의한 업무 오류(ROW_LIMIT_EXCEEDED · ARG_INVALID 등)는 인자 때문에 나므로 tool_arg_error로 센다.
  timeout · 429 · 권한 거부 · 런타임 오류만 플랫폼 원인으로 빼서 따로 집계한다.
- 판정 기준: 최소 30 run · 기준 대비 1.5배 · +5%p · 단측 이항 p < 0.01, 기준선 바닥 1%.
- 같은 결함을 묶는 키는 버전을 뺀 자산 + 패턴이다(릴리스로 버전이 바뀌어도 이어 센다).
"""
from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict

from . import registry, tracing

# 개설 임계 (① LLD v0.1 7.3)
MIN_RUNS, RATE_MULT, MIN_ABS_DELTA, ALPHA, BASELINE_FLOOR = 30, 1.5, 0.05, 0.01, 0.01
PLATFORM_ERRORS = {"TIMEOUT", "RATE_LIMITED", "PERMISSION_DENIED", "RUNTIME_ERROR"}
LAB_PATTERNS = {"eval_fail", "no_asset_match"}       # 기준 문서 5종 밖, lab 전용


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k), X ~ Binomial(n, p). 단측 이항 검정."""
    import math
    if k <= 0:
        return 1.0
    return min(1.0, sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def _ver(refs, base):
    return next((r for r in refs if r.startswith(base + "@")), base)


def _rates(batch):
    runs = [r for r in tracing.read("runs_prod") if r.get("batch") == batch]
    sessions = [s for s in tracing.read("sessions") if s.get("batch") == batch]
    by_run = {r["run_id"]: r for r in runs}
    m = defaultdict(lambda: {"hits": 0, "n": 0, "users": set(), "evidence": []})
    platform = defaultdict(int)

    def platform_hit(r):
        return any(a.get("error") in PLATFORM_ERRORS for a in r["audit"])

    for r in runs:
        for a in r["audit"]:
            if not a["tool"].startswith("asset://tool/"):
                continue
            tref = _ver(r["asset_refs"], a["tool"])
            if a["status"] == "error" and a["error"] in PLATFORM_ERRORS:
                platform[(a["error"], tref)] += 1          # 플랫폼 원인: 분자 · 분모 모두에서 뺀다
                continue
            m[("tool_arg_error", tref)]["n"] += 1
            if a["status"] == "error":
                k = m[("tool_arg_error", tref)]
                k["hits"] += 1
                k["users"].add(r["user_id"])
                if len(k["evidence"]) < 50:
                    k["evidence"].append({"run_id": r["run_id"], "error": a["error"], "grade": 0})
        if platform_hit(r):
            continue
        aref = _ver(r["asset_refs"], registry.DEFAULT_AGENT)
        k = m[("limit_exceeded", aref)]
        k["n"] += 1
        if r["status"] == "budget_exceeded":
            k["hits"] += 1
            k["users"].add(r["user_id"])
            k["evidence"].append({"run_id": r["run_id"], "grade": 0})

    for s in sessions:
        r = by_run.get(s["run_id"])
        if not r or platform_hit(r):
            continue
        aref = _ver(r["asset_refs"], registry.DEFAULT_AGENT)
        k = m[("repeat_question", aref)]
        k["n"] += 1
        if s["events"]:
            k["hits"] += 1
            k["users"].add(s["user_id"])
            if len(k["evidence"]) < 50:
                k["evidence"].append({"run_id": s["run_id"], "events": s["events"], "grade": 0})
        if s.get("procedure") and not s.get("covered_by_skill"):
            k2 = m[("no_asset_match", s["procedure"])]
            k2["hits"] += 1
            k2["users"].add(s["user_id"])
            k2["evidence"].append({"run_id": s["run_id"], "grade": 0})
        k3 = m[("no_asset_match", "month_end_vendor_recon")]
        k3["n"] += 1
    # eval_fail(lab 전용): 정기 골든셋(고정 세트)에서 규칙 채점 통과율 하락. 대상은 실패 사례 유형의 자산
    for g in tracing.read("golden"):
        if g.get("batch") != batch:
            continue
        k = m[("eval_fail", g["agent_ref"])]
        k["n"] += 1
        if not g["passed"]:
            k["hits"] += 1
            k["users"].add(g["case_id"])          # 골든셋은 주체 대신 서로 다른 사례 수로 센다
            k["evidence"].append({"run_id": g["run_id"], "case_id": g["case_id"], "grade": 0,
                                  "failed_checks": sorted(x for x, v in (g.get("checks") or {}).items() if v is False)})
    return m, platform


HYPO = {"repeat_question": "missing_instruction", "limit_exceeded": "budget_overrun",
        "no_asset_match": "missing_asset", "eval_fail": "missing_instruction"}
FIDELITY = {"tool_arg_error": "full", "repeat_question": "full", "limit_exceeded": "full",
            "no_asset_match": "gateway", "eval_fail": "full"}


def _hypo(sig, code):
    if sig == "tool_arg_error":           # 형식 위반은 도구 설명, 업무 오류(ROW_LIMIT 등)는 절차 쪽이 흔한 원인
        return "schema_defect" if code == "ARG_INVALID" else "description_ambiguity"
    return HYPO[sig]


def _fidelity(sig, code):
    return "gateway" if sig == "tool_arg_error" and code == "ARG_INVALID" else FIDELITY[sig]


def analyze(batch: str, baseline_batch: str | None = None) -> dict:
    cur, platform = _rates(batch)
    base, _ = _rates(baseline_batch) if baseline_batch else ({}, {})
    findings, below = [], []
    for (sig, target), k in cur.items():
        if not k["n"] or not k["hits"]:
            continue
        rate = k["hits"] / k["n"]
        b = base.get((sig, target)) or base.get((sig, target.split("@")[0]))
        # 기준선은 같은 자산의 이전 버전(정상)에서 잰다 (① LLD 7.2: 새 버전은 직전 버전 기준선을 이어받음)
        if b is None and baseline_batch:
            b = next((v for (s2, t2), v in base.items() if s2 == sig and t2.split("@")[0] == target.split("@")[0]), None)
        brate = max((b["hits"] / b["n"]) if b and b["n"] else 0.0, BASELINE_FLOOR)
        if sig == "no_asset_match":                       # lab 전용: 맞는 자산이 없는 요청이 반복
            ok = k["hits"] >= MIN_RUNS // 3
        elif sig == "eval_fail":                          # lab 전용: 골든셋은 규모가 작아 기준 대비 하락으로 본다
            ok = k["hits"] >= 2 and rate >= MIN_ABS_DELTA and rate > brate
        else:
            ok = (k["n"] >= MIN_RUNS and rate >= RATE_MULT * brate and rate - brate >= MIN_ABS_DELTA
                  and binom_sf(k["hits"], k["n"], brate) < ALPHA)
        (findings if ok else below).append(_finding(sig, target, k, rate, brate, batch, baseline_batch))
    plat = [{"error": e, "tool": t, "count": c} for (e, t), c in platform.items()]
    return {"findings": findings, "below_threshold": below, "platform_signals": plat}


def _finding(sig, target, k, rate, brate, batch, baseline_batch):
    """① finding 스키마 v1(기준 문서 7개 필드 + ① 확장) + ②가 요청한 dedupe_key · signature.
    실험장에만 필요한 값은 _lab 아래에 둔다(실제 ①에는 없다)."""
    is_new = sig == "no_asset_match"
    ev = k["evidence"]
    signature = None if is_new else _signature(sig, target, ev)
    code = (signature or {}).get("result_code")
    base = target.split("@")[0]
    from .config import TODAY
    day = TODAY.isoformat()
    return {
        "finding_id": f"fnd_{day[:4]}_{day[5:7]}_{int(uuid.uuid4().hex[:8], 16) % 10**8:08d}",
        "asset_ref": None if is_new else target,
        "pattern": sig,
        "window": {"from": f"{day}T00:00:00Z", "to": f"{day}T23:59:59Z"},
        "metrics": {"runs": k["n"], "fail_rate": round(rate, 4), "baseline_fail_rate": round(brate, 4)},
        "examples": [e["run_id"] for e in ev if e.get("run_id")][:5],
        "hint": _hypo(sig, code),
        "schema_version": "1",
        "attribution": "asset", "fixable_by": "sel", "confidence": "ok",
        "affected_requests": k["hits"], "recurrence": 0,
        "dedupe_key": f"{base}|{sig}",
        "signature": signature,
        "_lab": {"analyzer": "fallback-0.2", "batch": batch, "baseline_batch": baseline_batch,
                 "fidelity": _fidelity(sig, code), "new_asset": {"kind": "skill", "procedure": target} if is_new else None,
                 "distinct_principals": len(k["users"])},
    }


def failing_runs(finding: dict) -> list[dict]:
    """②가 ①의 정규화 기록(sel_run)을 읽는 자리. finding의 자산 · 패턴 · 구간에 해당하는 실패 run 전부.
    실제로는 SEL 저장소의 sel_run을 직접 읽는다(DBC 아님. finding에는 예시 5개만 실린다)."""
    lab = finding.get("_lab") or {}
    if not lab.get("batch"):
        return []
    cur, _ = _rates(lab["batch"])
    key = (finding["pattern"], finding["asset_ref"] or (lab.get("new_asset") or {}).get("procedure"))
    return cur.get(key, {}).get("evidence", [])


def _signature(sig, target, evidence):
    from .testset import finding_signature
    return finding_signature(sig, target, evidence)
