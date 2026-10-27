"""폴백 분석기 (앱 설계 5.3). 운영 실행 기록·Gateway 감사 기록·세션 기록을 집계해 finding을 낸다.

규칙: R1 tool_error, R2 arg_invalid, R5 negative_feedback, R6 eval_fail, R7 no_asset_match, R8 budget_exceeded.
(R3 retry_loop, R4 hitl_rejected는 v0에서 생략)
플랫폼 원인(TIMEOUT 등)은 자산 finding에서 빼고 플랫폼 신호로 따로 집계한다 (5.2).
"""
from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict

from . import tracing

# 개설 임계 (앱 설계 5.2 기본값)
MIN_SUPPORT, MIN_PRINCIPALS, RATE_MULT, RATE_ABS, RATE_ABS_NEW = 20, 3, 1.5, 0.02, 0.05
PLATFORM_ERRORS = {"TIMEOUT"}


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
            if not a["tool"].startswith("tool/"):
                continue
            tref = _ver(r["asset_refs"], a["tool"])
            for sig in ("tool_error", "arg_invalid"):
                m[(sig, tref)]["n"] += 1
            if a["status"] == "error":
                if a["error"] in PLATFORM_ERRORS:
                    platform[(a["error"], tref)] += 1
                    continue
                sig = "arg_invalid" if a["error"] == "ARG_INVALID" else "tool_error"
                k = m[(sig, tref)]
                k["hits"] += 1
                k["users"].add(r["user_id"])
                if len(k["evidence"]) < 50:
                    k["evidence"].append({"run_id": r["run_id"], "error": a["error"], "grade": 0})
        if platform_hit(r):
            continue
        aref = _ver(r["asset_refs"], "agent/fi-assistant")
        k = m[("budget_exceeded", aref)]
        k["n"] += 1
        if r["status"] == "budget_exceeded":
            k["hits"] += 1
            k["users"].add(r["user_id"])
            k["evidence"].append({"run_id": r["run_id"], "grade": 0})

    for s in sessions:
        r = by_run.get(s["run_id"])
        if not r or platform_hit(r):
            continue
        aref = _ver(r["asset_refs"], "agent/fi-assistant")
        k = m[("negative_feedback", aref)]
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
    # R6 eval_fail: 정기 골든셋(고정 세트)에서 규칙 채점 통과율 하락. 대상은 실패 사례 유형의 자산
    for g in tracing.read("golden"):
        if g.get("batch") != batch:
            continue
        k = m[("eval_fail", g["agent_ref"])]
        k["n"] += 1
        if not g["passed"]:
            k["hits"] += 1
            k["users"].add(g["case_id"])          # 골든셋은 주체 대신 서로 다른 사례 수로 센다
            k["evidence"].append({"run_id": g["run_id"], "case_id": g["case_id"], "grade": 0})
    return m, platform


HYPO = {"tool_error": "description_ambiguity", "arg_invalid": "schema_defect", "negative_feedback": "missing_instruction",
        "budget_exceeded": "budget_overrun", "no_asset_match": "missing_asset", "eval_fail": "missing_instruction"}
FIDELITY = {"tool_error": "full", "arg_invalid": "gateway", "negative_feedback": "full", "budget_exceeded": "full",
            "no_asset_match": "gateway", "eval_fail": "full"}


def analyze(batch: str, baseline_batch: str | None = None) -> dict:
    cur, platform = _rates(batch)
    base, _ = _rates(baseline_batch) if baseline_batch else ({}, {})
    findings, below = [], []
    for (sig, target), k in cur.items():
        if not k["n"] or not k["hits"]:
            continue
        rate = k["hits"] / k["n"]
        b = base.get((sig, target)) or base.get((sig, target.split("@")[0]))
        # 기준선은 같은 자산의 이전 버전(정상)에서 잰다
        if b is None and baseline_batch:
            b = next((v for (s2, t2), v in base.items() if s2 == sig and t2.split("@")[0] == target.split("@")[0]), None)
        brate = (b["hits"] / b["n"]) if b and b["n"] else None
        if sig == "no_asset_match":
            support = k["hits"]
            ok_rate = True
        elif sig == "eval_fail":       # 골든셋은 규모가 작아 지지 건수 대신 기준 대비 하락으로 본다
            support = k["hits"]
            ok_rate = rate >= RATE_ABS_NEW and (brate is None or rate > brate)
            ok = support >= 2 and ok_rate
            (findings if ok else below).append(_finding(sig, target, k, rate, brate, batch, baseline_batch, support))
            continue
        else:
            support = k["hits"]
            ok_rate = (rate >= RATE_ABS and rate >= RATE_MULT * brate) if brate else rate >= RATE_ABS_NEW
        ok = support >= MIN_SUPPORT and len(k["users"]) >= MIN_PRINCIPALS and ok_rate
        (findings if ok else below).append(_finding(sig, target, k, rate, brate, batch, baseline_batch, support))
    plat = [{"error": e, "tool": t, "count": c} for (e, t), c in platform.items()]
    return {"findings": findings, "below_threshold": below, "platform_signals": plat}


def _finding(sig, target, k, rate, brate, batch, baseline_batch, support):
        is_new = sig == "no_asset_match"
        return {
            "finding_id": f"fnd-{uuid.uuid4().hex[:10]}",
            "analyzer": {"name": "fallback", "version": "0.1.0"}, "window": {"batch": batch, "baseline": baseline_batch},
            "fidelity": FIDELITY[sig], "signal": sig,
            "target": {"asset_ref": None if is_new else target, "channel": "runtime", **({"proposed_kind": "skill", "procedure": target} if is_new else {})},
            "hypothesis_class": HYPO[sig],
            "metrics": {"support": support, "distinct_principals": len(k["users"]), "rate": round(rate, 4),
                        "baseline_rate": round(brate, 4) if brate is not None else None, "n": k["n"]},
            "evidence": k["evidence"][:50], "max_grade": 0,
            "dedupe_key": hashlib.sha1(f"{target.split('@')[0]}|{sig}|{HYPO[sig]}".encode()).hexdigest()[:16],
        }
