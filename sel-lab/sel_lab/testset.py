"""W9 · ② Creator 단계의 하네스: 생성한 시험 질문을 지금 버전으로 돌려 거르고 잠근다 (docs/16 6장).

질문은 testgen이 finding만 보고 새로 만든다. 무엇을 남길지는 LLM이 아니라 탐침 실행 결과와 이 코드가 정한다.

1 재료: testgen이 만든 질문(target_ref를 주면 그 자산을 쓰는 업무로 좁힌다. 지금 루프는 target_ref 없이 부른다). 표본 상한 N
2 기준 실패 탐침: 현재 버전으로 2회. 2회 중 1회만 실패하면 3회째
  - 목표(past_failure): 실패 서명이 finding과 같은 실패가 2회 이상 (서명 없는 신호는 실패 2회 이상)
  - 정상: 2회 모두 통과
  - 나머지(경계에서 못 넘음, 서명 다른 실패)는 제외
3 필수 · 정책(고정) 케이스는 매번 전부 (사람이 만든 고정 세트)
4 정상 문제는 업무 유형별로 고르게(층화). 범위 안에서 모자라면 범위 밖 업무에서 채운다
5 고정: 세트 ID · frozen_at · 항목. 이후 바꿀 수 없다

탐침 결과는 거르는 데만 쓰고 판정은 ③(lab: tester.paired_run)이 기존 · 후보를 새로 짝 실행한다.

예전 문제은행 방식의 코드가 남아 있다(정리 대상, README "PRD와 다른 점"):
- 서명 일치가 MIN_SIG_MATCH 미만이면 반복 실패로 대신 채우고 rule에 남긴다. 설계는 insufficient_set으로 끝낸다.
- 사용 횟수 상한(USAGE_CAP · case_usage)과 노출 시 세트 무효화(invalidate_exposed). 생성 질문은 매번 새 ID라 상한에 걸리지 않고,
  루프는 노출 기록(sel.expose)을 부르지 않는다. 노출 원장은 GEPA를 켤 때만 필요하다.
"""
from __future__ import annotations

import hashlib
import json
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import registry, tester

USAGE_CAP = 50
MIN_SIG_MATCH = 2          # 서명 일치 실패가 이보다 적으면 반복 실패로 대체한다(예전 동작. 설계는 insufficient_set)
N_PROBE = 200
K_PROBE = 2


# ---------- 실패 서명 ----------

def finding_signature(signal: str, target: str, evidence: list[dict]) -> dict | None:
    """①이 finding에 붙이는 서명 (docs/09 C1). 서명을 정할 수 없는 신호는 None."""
    base = registry.base_of(target) if target else None
    if signal == "tool_arg_error":                # 도구가 돌려준 업무 오류 · 형식 오류 (결과 코드로 구분)
        codes = Counter(e.get("error") for e in evidence if e.get("error"))
        return {"outcome_type": "arg_error", "tool": base, "result_code": codes.most_common(1)[0][0]} if codes else None
    if signal == "limit_exceeded":
        return {"outcome_type": "limit_exceeded"}
    if signal == "eval_fail":
        checks = sorted({c for e in evidence for c in e.get("failed_checks", [])})
        return {"outcome_type": "eval_fail", "failed_checks": checks} if checks else None
    return None                                   # repeat_question 등: 서명 없음


def matches(sig: dict | None, run: dict) -> bool:
    """탐침 실행 하나가 서명과 같은 방식으로 실패했는가. run은 tester.run_case 결과."""
    if run.get("passed") is not False:
        return False
    if sig is None:
        return True
    t = sig["outcome_type"]
    if t == "arg_error":
        return any(e["tool"] == sig["tool"] and e["error"] == sig["result_code"] for e in run.get("errors", []))
    if t == "limit_exceeded":
        return run.get("status") == "budget_exceeded"
    if t == "eval_fail":
        return bool({k for k, v in run.get("checks", {}).items() if v is False} & set(sig["failed_checks"]))
    return False


# ---------- 범위 ----------

def scope_kinds(target_ref: str | None, kinds_map: dict) -> set[str]:
    """대상 자산을 쓰는 업무 유형. kinds_map: {업무 유형: (자산 base, 설명)} (sel.KINDS)."""
    all_kinds = set(kinds_map)
    if not target_ref:
        return all_kinds
    base, kind = registry.base_of(target_ref), registry.kind_of(target_ref)
    if kind == "skill":
        return {k for k, (b, _) in kinds_map.items() if b == base} or all_kinds
    if kind == "tool":
        bundle = registry.resolve_agent()
        skills = {s.base for s in bundle["skills"] if any(registry.base_of(t) == base for t in s.meta.get("allowed_tools", []))}
        return {k for k, (b, _) in kinds_map.items() if b in skills} or all_kinds
    return all_kinds                              # agent · prompt: 모든 업무


# ---------- 탐침 · 분류 ----------

def classify(runs: list[dict], sig: dict | None) -> str:
    """normal(2회 모두 통과) · past_failure(서명 일치 실패 2회 이상, 서명 없으면 실패 2회 이상)
    · repeat_failure(서명은 다르지만 실패 2회 이상: 대체 후보) · excluded."""
    valid = [r for r in runs if r.get("passed") is not None]
    if len(valid) < 2:
        return "excluded"
    if all(r["passed"] for r in valid[:2]) and len(valid) == 2:
        return "normal"
    if sum(1 for r in valid if matches(sig, r)) >= 2:
        return "past_failure"
    if sig is not None and sum(1 for r in valid if r["passed"] is False) >= 2:
        return "repeat_failure"
    return "excluded"


def probe(case: dict, sig: dict | None, runner=None) -> tuple[str, list[dict]]:
    runner = runner or tester.run_case
    runs = [runner(case, None, arm="probe", repeat=r) for r in range(K_PROBE)]
    fails = sum(1 for r in runs if r.get("passed") is False)
    if fails == 1:                                # 경계: 3회째
        runs.append(runner(case, None, arm="probe", repeat=K_PROBE))
    return classify(runs, sig), runs


# ---------- 선정 · 고정 ----------

def _order(seed: str, tag: str, ids):
    return sorted(ids, key=lambda x: zlib.crc32(f"{tag}|{seed}|{x}".encode()))


def stratify(ids: list[str], cases: dict, n: int, seed: str) -> list[str]:
    by = defaultdict(list)
    for cid in _order(seed, "guard", ids):
        by[cases[cid]["kind"]].append(cid)
    out, kinds = [], sorted(by)
    while len(out) < n and any(by[k] for k in kinds):
        for k in kinds:
            if by[k] and len(out) < n:
                out.append(by[k].pop(0))
    return out


def select(held: set[str], *, target_ref: str | None, finding: dict | None, kinds_map: dict, usage: dict,
           seed: str, n_target=12, n_guard=8, n_probe=N_PROBE, runner=None, log=print) -> dict:
    cases = tester.cases()
    sig = (finding or {}).get("signature")
    scope = scope_kinds(target_ref, kinds_map)
    pool = [cid for cid in held if cases[cid]["kind"] in scope and usage.get(cid, 0) < USAGE_CAP]
    pool = _order(seed, "probe", pool)[:n_probe]
    cls, nrun = defaultdict(list), 0
    for cid in pool:
        c, runs = probe(cases[cid], sig, runner)
        cls[c].append(cid)
        nrun += len(runs)
    rule = "signature" if sig else "repeat_failure(no signature)"
    past = cls["past_failure"]
    if sig is not None and len(past) < MIN_SIG_MATCH:
        # 예전 문제은행 방식에서 생긴 대체 규칙(D01: ROW_LIMIT 재현 사례가 전부 노출 원장으로 빠져 일치 0건).
        # 설계는 이 경우 insufficient_set으로 끝낸다(docs/16 6장). 코드는 아직 반복 실패한 질문으로 채우고
        # 그 사실을 rule에 남긴다(희석 위험). 정리 대상.
        past = past + cls["repeat_failure"]
        rule = "repeat_failure(fallback: signature matched %d)" % len(cls["past_failure"])
    target = _order(seed, "target", past)[:n_target]
    if finding and finding.get("pattern") == "eval_fail":       # lab 전용 eval_fail: 실패한 고정 사례도 목표 (질문 문장은 Creator에 안 갔다)
        from .analyzer import failing_runs
        target = sorted({e["case_id"] for e in failing_runs(finding) if e.get("case_id")}) + target
    guard = stratify(cls["normal"], cases, n_guard, seed)
    out_normal = []
    if len(guard) < n_guard:
        # 범위 안 입력이 모두 결함에 걸리면 정상 문제가 없다. 범위 밖 입력에서 정상 문제를 채운다
        # (공유 자산을 고친 변경이 다른 업무를 깨뜨리지 않는지도 같이 본다).
        outside = _order(seed, "outside", [cid for cid in held if cases[cid]["kind"] not in scope
                                            and usage.get(cid, 0) < USAGE_CAP])[:(n_guard - len(guard)) * 4]
        for cid in outside:
            c, runs = probe(cases[cid], sig, runner)
            nrun += len(runs)
            if c == "normal":
                out_normal.append(cid)
        guard = guard + stratify(out_normal, cases, n_guard - len(guard), seed)
    fixed = [cid for cid, c in sorted(cases.items()) if c["fixed"] and cid not in target]
    log(f"  test set: 범위 {len(pool)}건(업무 {len(scope)}종) · 탐침 {nrun}회 · 서명 일치 실패 {len(cls['past_failure'])} · "
        f"반복 실패 {len(cls['repeat_failure'])} · 정상 {len(cls['normal'])} · 제외 {len(cls['excluded'])} "
        f"→ 목표 {len(target)} · 정상 {len(guard)}(범위 밖 {len(guard) - len([g for g in guard if g in cls['normal']])}) · 고정 {len(fixed)} [{rule}]")
    return {"target": target, "guard": guard, "fixed": fixed,
            "_stats": {"scope": len(pool), "probe_runs": nrun, "signature_matched": len(cls["past_failure"]),
                       "repeat_failure": len(cls["repeat_failure"]), "normal": len(cls["normal"]),
                       "excluded": len(cls["excluded"]), "rule": rule,
                       "guard_out_of_scope": len([g for g in guard if g not in cls["normal"]])}}


def input_hash(text: str) -> str:
    return hashlib.sha1(" ".join((text or "").split()).encode()).hexdigest()[:16]


def freeze(conn, sel_case_id: str, iteration: int, eval_set: dict) -> dict:
    """세트를 고정한다. 반환: {id, frozen_at}. 항목은 이후 바꿀 수 없다(갱신 API 없음)."""
    cases = tester.cases()
    items = [{"id": cid, "set": kind, "hash": input_hash(cases[cid]["input"]["user_message"])}
             for kind in ("target", "guard", "fixed") for cid in eval_set[kind]]
    body = json.dumps(items, ensure_ascii=False, sort_keys=True)
    sid = "ts-" + hashlib.sha1(f"{sel_case_id}|{iteration}|{body}".encode()).hexdigest()[:10]
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("insert or ignore into testset values(?,?,?,?,?,?)", (sid, sel_case_id, iteration, now, "frozen", body))
    conn.commit()
    return {"id": sid, "frozen_at": now}


def status(conn, sid: str) -> str:
    r = conn.execute("select status from testset where id=?", (sid,)).fetchone()
    return r[0] if r else "missing"


def invalidate_exposed(conn, hashes: set[str]) -> list[str]:
    """노출 기록 시점에 검사: 판정 전(frozen) 세트에 노출된 입력이 있으면 무효로 한다.
    예전 노출 원장의 잔재다. 루프는 sel.expose를 부르지 않아 지금은 테스트에서만 쓰인다."""
    hit = []
    for sid, body in conn.execute("select id, items from testset where status='frozen'").fetchall():
        if any(it["hash"] in hashes for it in json.loads(body)):
            hit.append(sid)
    for sid in hit:
        conn.execute("update testset set status='invalid' where id=?", (sid,))
    conn.commit()
    return hit


def mark_judged(conn, sid: str, eval_set: dict) -> None:
    conn.execute("update testset set status='judged' where id=? and status='frozen'", (sid,))
    for cid in eval_set["target"] + eval_set["guard"]:            # 고정 세트는 바닥 검사라 사용 상한을 세지 않는다
        conn.execute("insert into case_usage values(?,1) on conflict(eval_case_id) do update set uses=uses+1", (cid,))
    conn.commit()


def usage_map(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute("select eval_case_id, uses from case_usage")}
