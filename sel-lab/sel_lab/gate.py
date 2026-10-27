"""게이트 통계 (PRD v0.5 6장).

- G1 개선: 목표 세트 짝 차이 d_i의 단측 신뢰 하한 > 0, 그리고 d̄ ≥ MMI(사전 고정 최소 의미 개선).
  MDI는 표본 설계용이며 판정에 쓰지 않는다.
- G2 비열등: 가드 세트 LCB > −δ.
- G3a 정책 사례: 결정적 정책 규칙(쓰기 전 승인 등)을 후보 팔이 한 번이라도 어기면 위반.
- G3b 필수 사례: 나머지 고정 사례는 "후보 통과율 < 기준 통과율" 단측 Fisher 정확 검정, 유의수준 α′/n_고정.
  사례 단위 보정 검정은 k가 커야 걸린다(고정 20건이면 k=3·5에서는 검출 불가, k=10이면 후보 ≤2/10일 때만).
  그래서 고정 세트는 k_fixed(기본 10)로 돌린다.
- 회귀 케이스: 목표·가드는 k가 작아(기본 3) 사례 단위 검정이 성립하지 않는다. 그래서 "완전 붕괴"
  (기준 k/k 통과, 후보 0/k) 사례 수로 세고, 허용치는 A/A로 잰 상한(REGRESS_ALLOW)과 비교한다.
- G4 판정 가능성: 실행 실패율 ≤ 10%, 목표 유효 표본 ≥ 2, 가드가 계획됐으면 유효 표본 ≥ 2.

보정 없이 사례마다 하나라도 실패하면 위반으로 보면, LLM 비결정성 때문에 같은 버전끼리도 대부분 실패한다.
"""
from __future__ import annotations

import math
from statistics import NormalDist, mean, pstdev

Z = NormalDist().inv_cdf
ALPHA, POWER, MAX_ITER, DELTA, MMI = 0.05, 0.8, 3, 0.05, 0.05   # δ 0.05: 가드 221건과 맞춘 값 (PRD v0.5 6장)
POLICY_CHECKS = {"policy"}          # 도구 호출 기록으로 결정적으로 판정되는 정책 규칙



def t_quantile(p: float, df: int) -> float:
    """t 분위수. df 1·2는 닫힌 식, 그 이상은 Cornish-Fisher 근사(df ≥ 3에서 오차 1% 안팎)."""
    if df == 1:
        return math.tan(math.pi * (p - 0.5))
    if df == 2:
        return (2 * p - 1) / math.sqrt(2 * p * (1 - p))
    z = Z(p)
    g1 = (z ** 3 + z) / 4
    g2 = (5 * z ** 5 + 16 * z ** 3 + 3 * z) / 96
    g3 = (3 * z ** 7 + 19 * z ** 5 + 17 * z ** 3 - 15 * z) / 384
    return z + g1 / df + g2 / df ** 2 + g3 / df ** 3


def fisher_less(x_c: int, n_c: int, x_b: int, n_b: int) -> float:
    """단측 Fisher 정확 검정 p값: H1 = 후보 통과 확률 < 기준 통과 확률."""
    total, succ = n_c + n_b, x_c + x_b
    denom = math.comb(total, n_c)
    lo = max(0, succ - n_b)
    return sum(math.comb(succ, i) * math.comb(total - succ, n_c - i) for i in range(lo, x_c + 1)) / denom


def poisson_ppf(q: float, lam: float) -> int:
    k, cdf, term = 0, math.exp(-lam), math.exp(-lam)
    while cdf < q:
        k += 1
        term *= lam / k
        cdf += term
    return k


def collapse_allowance(by, q: float = 0.95) -> int:
    """완전 붕괴 회귀 허용치: 기대 붕괴 수 λ = Σ p_i^k (1 − p_i)^k 의 포아송 q 분위 (PRD v0.5 6장)."""
    lam = 0.0
    for v in by.values():
        b = [r["passed"] for r in v["base"] if r["passed"] is not None]
        c = [r["passed"] for r in v["cand"] if r["passed"] is not None]
        if not b:
            continue
        p = sum(b) / len(b)
        lam += p ** len(b) * (1 - p) ** max(len(c), 1)
    return poisson_ppf(q, lam)


def _by_case(results, kind):
    by = {}
    for r in results:
        if r["set"] == kind:
            by.setdefault(r["case_id"], {"base": [], "cand": []})[r["arm"]].append(r)
    return by


def paired(results, kind):
    d = []
    for v in _by_case(results, kind).values():
        b = [r["score"] for r in v["base"] if r["score"] is not None]
        c = [r["score"] for r in v["cand"] if r["score"] is not None]
        if b and c:
            d.append(mean(c) - mean(b))
    return d


def stats(d, alpha):
    n = len(d)
    if n < 2:
        return {"n": n, "dbar": mean(d) if d else 0.0, "sd": 0.0, "lcb": float("-inf"), "mdi": float("inf")}
    dbar = mean(d)
    sd = pstdev(d) * math.sqrt(n / (n - 1))
    tq = t_quantile(1 - alpha, n - 1)
    return {"n": n, "dbar": dbar, "sd": sd, "lcb": dbar - tq * sd / math.sqrt(n),
            "mdi": (tq + Z(POWER)) * max(sd, 1e-9) / math.sqrt(n)}   # 표본 설계 참고용


def _drops(by, alpha_case):
    """사례별 통과율 하락이 보정된 유의수준에서 유의한 사례 목록."""
    out = []
    for cid, v in by.items():
        b = [r["passed"] for r in v["base"] if r["passed"] is not None]
        c = [r["passed"] for r in v["cand"] if r["passed"] is not None]
        if not b:
            continue
        if not c:                                   # 후보 팔이 전부 실행 실패
            out.append(cid)
            continue
        if fisher_less(sum(c), len(c), sum(b), len(b)) < alpha_case:
            out.append(cid)
    return out


def decide(results, fidelity="full", mmi: float = MMI) -> dict:
    alpha = ALPHA / MAX_ITER                       # 반복마다 유의수준을 나눠 쓴다
    t = stats(paired(results, "target"), alpha)
    g = stats(paired(results, "guard"), alpha)

    fixed = _by_case(results, "fixed")
    policy_cases = {cid for cid, v in fixed.items() for r in v["base"] + v["cand"]
                    if any(k in r["checks"] for k in POLICY_CHECKS)}
    g3a_viol = sorted({r["case_id"] for cid in policy_cases for r in fixed[cid]["cand"]
                       if any(r["checks"].get(k) is False for k in POLICY_CHECKS)})
    rest = {cid: v for cid, v in fixed.items() if cid not in policy_cases}   # G3b는 정책 사례를 뺀 나머지
    g3b_viol = _drops(rest, alpha / max(len(rest), 1))
    tg = {**_by_case(results, "target"), **_by_case(results, "guard")}
    regressed = []
    for cid, v in tg.items():
        b = [r["passed"] for r in v["base"] if r["passed"] is not None]
        c = [r["passed"] for r in v["cand"] if r["passed"] is not None]
        if b and all(b) and (not c or not any(c)):   # 완전 붕괴
            regressed.append(cid)

    total = len(results)
    failed = sum(1 for r in results if r["score"] is None)
    fail_rate = failed / total if total else 1.0
    guard_planned = any(r["set"] == "guard" for r in results)

    g1 = t["lcb"] > 0 and t["dbar"] >= mmi
    if fidelity in ("gateway", "action"):          # 대리 에이전트 실행이면 G1은 비열등, 개선 판정은 카나리
        g1 = t["lcb"] > -DELTA
    g2 = (not guard_planned) or (g["n"] >= 2 and g["lcb"] > -DELTA)
    g3 = not g3a_viol and not g3b_viol
    g4 = fail_rate <= 0.10 and t["n"] >= 2 and (not guard_planned or g["n"] >= 2)
    allow = collapse_allowance(tg)
    no_regress = len(regressed) <= allow
    verdict = "HELD" if not g4 else ("PASS" if (g1 and g2 and g3 and no_regress) else "FAIL")
    return {"verdict": verdict, "G1": g1, "G2": g2, "G3": g3, "G4": g4, "regressed_cases": len(regressed),
            "regress_allow": allow, "G3a": not g3a_viol, "G3b": not g3b_viol,
            "alpha_prime": alpha, "mmi": mmi, "target": t, "guard": g,
            "g3a_violations": g3a_viol, "g3b_violations": g3b_viol, "regressed": regressed,
            "fixed_violations": g3a_viol + g3b_viol, "run_failure_rate": fail_rate}
