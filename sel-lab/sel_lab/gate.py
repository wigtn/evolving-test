"""게이트 통계 (앱 설계 7장).

질문은 "후보가 기준보다 나은가"가 아니라 "관측된 차이가 노이즈와 표본 크기로 설명되지 않을 만큼 큰가"다.
사례 i마다 d_i = mean(후보 점수) - mean(기준 점수). 단측 신뢰 하한(LCB)과 MDI로 판정한다.
"""
from __future__ import annotations

import math
from statistics import NormalDist, mean, pstdev

Z = NormalDist().inv_cdf


def t_quantile(p: float, df: int) -> float:
    """t 분위수 근사 (Cornish-Fisher, scipy 없이). 소표본에서 z보다 보수적이다."""
    z = Z(p)
    g1 = (z ** 3 + z) / 4
    g2 = (5 * z ** 5 + 16 * z ** 3 + 3 * z) / 96
    g3 = (3 * z ** 7 + 19 * z ** 5 + 17 * z ** 3 - 15 * z) / 384
    return z + g1 / df + g2 / df ** 2 + g3 / df ** 3
ALPHA, POWER, MAX_ITER, DELTA = 0.05, 0.8, 3, 0.02


def paired(results, kind):
    """results: [{case_id, kind_of_set, arm, score}] → 사례별 d_i."""
    by = {}
    for r in results:
        if r["set"] != kind or r["score"] is None:
            continue
        by.setdefault(r["case_id"], {"base": [], "cand": []})[r["arm"]].append(r["score"])
    d = [mean(v["cand"]) - mean(v["base"]) for v in by.values() if v["base"] and v["cand"]]
    return d


def stats(d, alpha):
    n = len(d)
    if n < 2:
        return {"n": n, "dbar": mean(d) if d else 0.0, "sd": 0.0, "lcb": float("-inf"), "mdi": float("inf")}
    dbar = mean(d)
    sd = pstdev(d) * math.sqrt(n / (n - 1))
    se = sd / math.sqrt(n)
    za, zb = t_quantile(1 - alpha, n - 1), Z(POWER)
    sd_eff = sd if sd > 0 else 1e-9
    return {"n": n, "dbar": dbar, "sd": sd, "lcb": dbar - za * se, "mdi": (za + zb) * sd_eff / math.sqrt(n)}


def decide(results, fidelity="full") -> dict:
    alpha = ALPHA / MAX_ITER                       # 반복마다 유의수준을 나눠 쓴다 (7.3)
    t = stats(paired(results, "target"), alpha)
    g = stats(paired(results, "guard"), alpha)

    # G3: 고정 세트에서 기준 팔이 통과한 규칙 채점을 후보 팔이 하나라도 실패하면 안 됨
    fixed_viol = []
    fx = {}
    for r in results:
        if r["set"] == "fixed" and r["passed"] is not None:
            fx.setdefault(r["case_id"], {"base": [], "cand": []})[r["arm"]].append(r["passed"])
    for cid, v in fx.items():
        if v["base"] and all(v["base"]) and (not v["cand"] or not all(v["cand"])):   # 후보 결과가 없으면 위반
            fixed_viol.append(cid)
    fixed_ids = {r["case_id"] for r in results if r["set"] == "fixed"}
    for cid in fixed_ids - set(fx):                                     # 두 팔 모두 실행 실패
        fixed_viol.append(cid)

    total = len(results)
    failed = sum(1 for r in results if r["score"] is None)
    fail_rate = failed / total if total else 1.0

    g1 = t["lcb"] > 0 and t["dbar"] >= t["mdi"]
    if fidelity in ("gateway", "action"):          # 대리 에이전트 실행이면 G1은 비열등, 개선 판정은 카나리
        g1 = t["lcb"] > -DELTA
    guard_planned = any(r["set"] == "guard" for r in results)
    g2 = (not guard_planned) or (g["n"] >= 2 and g["lcb"] > -DELTA)
    g3 = not fixed_viol
    g4 = fail_rate <= 0.10 and (not guard_planned or g["n"] != 1) and t["n"] >= 2   # 표본이 판정 불가면 HELD
    verdict = "HELD" if not g4 else ("PASS" if (g1 and g2 and g3) else "FAIL")
    return {"verdict": verdict, "G1": g1, "G2": g2, "G3": g3, "G4": g4, "alpha_prime": alpha,
            "target": t, "guard": g, "fixed_violations": fixed_viol, "run_failure_rate": fail_rate}
