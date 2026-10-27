"""Tester: 평가 세트의 각 사례를 기준 팔(현행 활성)과 후보 팔(eval.asset_overrides)로 k회씩 실행한다."""
from __future__ import annotations

import json
import zlib

from . import scorer, worker
from .config import DATA
from .simulator import CaseSimulator

_CASES = None


def cases() -> dict:
    global _CASES
    if _CASES is None:
        with (DATA / "eval" / "cases.jsonl").open(encoding="utf-8") as f:
            _CASES = {c["eval_case_id"]: c for c in map(json.loads, f)}
    return _CASES


def run_case(case: dict, overrides: dict | None, *, arm: str, repeat: int, source="eval") -> dict:
    r = worker.run(case["input"]["user_message"], user_id=case["input"]["user_id"], source=source,
                   overrides=overrides, simulator=CaseSimulator(case), case_id=case["eval_case_id"],
                   arm=arm, repeat=repeat, seed=zlib.crc32(f"{case['eval_case_id']}|{arm}|{repeat}".encode()) & 0xFFFF)
    s = scorer.score(case, r)
    return {"run_id": r["run_id"], "case_id": case["eval_case_id"], "arm": arm, "repeat": repeat,
            "score": s["score"], "passed": s["passed"], "checks": s["checks"], "status": r["status"]}


def paired_run(eval_set: dict, candidate_overrides: dict, k: int = 3, k_fixed: int = 10, log=print) -> list[dict]:
    """eval_set: {"target": [case_id], "guard": [...], "fixed": [...]}.
    고정 세트는 k_fixed로 돌린다. 사례 단위 보정 검정(G3b)은 k가 작으면 검출력이 없기 때문이다."""
    out = []
    for set_kind, ids in eval_set.items():
        kk = k_fixed if set_kind == "fixed" else k
        for cid in ids:
            c = cases()[cid]
            for rep in range(kk):
                for arm, ov in (("base", None), ("cand", candidate_overrides)):
                    row = run_case(c, ov, arm=arm, repeat=rep)
                    row["set"] = set_kind
                    out.append(row)
        log(f"  tester: {set_kind} {len(ids)}건 × {kk}회 × 2팔 완료")
    return out
