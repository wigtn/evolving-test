"""W3 · 공격 · 정상 시험 묶음 (docs/10 W3).

- dev 세트(tests/attacks/dev.yaml): 범주 a~g, 케이스마다 기대 규칙 ID. 기대 규칙으로 막혀야 통과(W3-1).
- 정상 세트: (1) 결함 카탈로그 정답 문구 (2) tests/attacks/normal.yaml. 하나라도 막히면 과잉 차단.
- 퍼징(W3-4): 표준 라이브러리 random(시드 고정)으로 우회 변형을 만든다. 사내에 pip가 없어 Hypothesis 대신 쓴다.
  변형: zero-width 삽입 · 호환 자모 분해 · 키릴 문자 · 전각 · 줄 분할 · Markdown 링크 · HTML 주석 · 코드 펜스 · 제목 삽입.
held-out(W3-3)과 변이 테스트(W3-5)는 검사기 v0 고정 뒤(12월) 따로 한다.
"""
from __future__ import annotations

import copy
import random
from collections import Counter
from pathlib import Path

import pytest
import yaml

from sel_lab import registry, sel
from sel_lab.governance import check

ROOT = Path(__file__).resolve().parents[1]
ATT = Path(__file__).parent / "attacks"
DEV = yaml.safe_load((ATT / "dev.yaml").read_text(encoding="utf-8"))
NORMAL = yaml.safe_load((ATT / "normal.yaml").read_text(encoding="utf-8"))
LIMITS = yaml.safe_load((ATT / "known_limits.yaml").read_text(encoding="utf-8"))
OVERBLOCK = yaml.safe_load((ATT / "known_overblock.yaml").read_text(encoding="utf-8"))
REFS = {**DEV["refs"], "TVS": "asset://tool/fi/vendor-search@1.0.0", "TOI": "asset://tool/fi/open-items-query@1.0.0"}


def build(case: dict) -> tuple[object, dict]:
    ref = REFS[case["ref"]]
    edits = []
    for e in case["edits"]:
        x = {"op": e["op"], "path": ref + e["at"]}
        if e["op"] == "set":
            x["value"] = e["value"]
        else:
            x["text"] = e["text"]
        edits.append(x)
    return registry.load(ref), {"base_ref": ref, "change_kind": case["kind"], "edits": edits}


def run(case: dict, bundle: dict | None = None):
    asset, b = build(case)
    return check(asset, bundle or b, change_kind=case["kind"], evidence=case.get("evidence", []),
                 fenced_removed=case.get("fenced_removed", []))


# ---------- dev ----------

@pytest.mark.parametrize("case", DEV["cases"], ids=[c["id"] for c in DEV["cases"]])
def test_dev_attack_blocked_by_expected_rule(case):
    d = run(case)
    assert not d.ok
    assert d.rules & set(case["expect"]), f"기대 {case['expect']} / 실제 {sorted(d.rules)}"


def test_dev_covers_all_categories():
    n = Counter(c["cat"] for c in DEV["cases"])
    assert set(n) == set("abcdefg")
    assert all(v >= 4 for v in n.values()), n
    assert len(DEV["cases"]) >= 40


def test_dev_ids_unique():
    ids = [c["id"] for c in DEV["cases"] + NORMAL["cases"] + LIMITS["cases"] + OVERBLOCK["cases"]]
    assert len(ids) == len(set(ids))


# ---------- 알려진 한계 (W3-7) ----------

@pytest.mark.parametrize("case", LIMITS["cases"], ids=[c["id"] for c in LIMITS["cases"]])
@pytest.mark.xfail(strict=True, reason="알려진 한계: 규칙 검사기 v0이 못 막는다(tests/attacks/known_limits.yaml)")
def test_known_limit(case):
    d = run(case)
    assert d.rules & set(case["expect"])


@pytest.mark.parametrize("case", OVERBLOCK["cases"], ids=[c["id"] for c in OVERBLOCK["cases"]])
@pytest.mark.xfail(strict=True, reason="알려진 과잉 차단: 정상 편집인데 v0이 막는다(tests/attacks/known_overblock.yaml)")
def test_known_overblock(case):
    assert run(case).ok


# ---------- 정상 세트 ----------

@pytest.mark.parametrize("case", NORMAL["cases"], ids=[c["id"] for c in NORMAL["cases"]])
def test_normal_edit_passes(case):
    d = run(case)
    assert d.ok, [(h.rule, h.detail) for h in d.hits]


def _catalog():
    return yaml.safe_load((ROOT / "data" / "defects" / "catalog.yaml").read_text(encoding="utf-8"))["defects"]


@pytest.mark.parametrize("did", ["D01", "D02", "D03", "D04", "D06", "D10"])
def test_catalog_fix_passes(did):
    """W3-6 (1): 카탈로그 정답 문구(검사기보다 먼저 쓰였다)는 0건 차단이어야 한다."""
    d = next(x for x in _catalog() if x["id"] == did)
    src = registry.load(f"{registry.base_of(d['target'])}@{d['degraded_version']}")
    out = sel._improver_oracle(src, d["expect"]["change_type"])
    r = check(src, out["edits"], change_kind=d["answer"]["change_kind"])
    assert r.ok, [(h.rule, h.detail) for h in r.hits]


def test_catalog_d05_is_governance_rejected():
    d = next(x for x in _catalog() if x["id"] == "D05")
    assert d["answer"]["c6"][-1] == "governance_rejected"
    src = registry.load(f"{registry.base_of(d['target'])}@{d['degraded_version']}")
    out = sel._improver_oracle(src, d["expect"]["change_type"])
    r = check(src, out["edits"], change_kind=d["answer"]["change_kind"])
    assert r.rules == {"SCOPE-ADDR"}


# ---------- 퍼징 ----------

CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
JONG = ["", *"ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"]
CYR = {"a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у", "i": "і"}


def _jamo(t, rng):
    out = []
    for ch in t:
        cp = ord(ch) - 0xAC00
        if 0 <= cp < 11172 and rng.random() < 0.7:
            out.append(CHO[cp // 588] + JUNG[(cp % 588) // 28] + JONG[cp % 28])
        else:
            out.append(ch)
    return "".join(out)


def _zw(t, rng):
    return "".join(c + ("​" if c.strip() and rng.random() < 0.4 else "") for c in t)


def _split(t, rng):
    lines = t.split("\n")
    i = rng.randrange(len(lines))
    ln = lines[i]
    if len(ln) > 4:
        k = rng.randrange(2, len(ln) - 1)
        if not ln[k:].lstrip().startswith(("#", "-", "=", "*", "+")):
            lines[i] = ln[:k] + "\n" + ln[k:]
    return "\n".join(lines)


TRANSFORMS = {
    "zero_width": _zw,
    "jamo": _jamo,
    "cyrillic": lambda t, rng: "".join(CYR.get(c, c) if rng.random() < 0.6 else c for c in t),
    "fullwidth": lambda t, rng: "".join(chr(ord(c) + 0xFEE0) if 0x21 <= ord(c) <= 0x7E and rng.random() < 0.7 else c for c in t),
    "bidi": lambda t, rng: "‮" + t.replace(" ", " ⁦", 3) + "‬",
    "line_split": _split,
    "md_link": lambda t, rng: "[" + t + "](#)",
    "html_comment": lambda t, rng: "<!-- " + t + " -->",
    "code_fence": lambda t, rng: "```\n" + t + "\n```",
    "heading": lambda t, rng: "### " + t,
}
WRAPS = {"md_link", "html_comment", "code_fence"}
# 퍼징 시드: 내용 규칙(inflow · safety · leak)으로 막히는 dev 케이스. 주소 · 섹션 규칙은 글 변형과 무관하다.
SEEDS = [c for c in DEV["cases"] if not any(r.startswith("SCOPE") for r in c["expect"])]


def _variant(case, name, rng):
    v = copy.deepcopy(case)
    for e in v["edits"]:
        key = "value" if e["op"] == "set" else "text"
        if isinstance(e.get(key), str):
            e[key] = TRANSFORMS[name](e[key], rng)
    return v


def fuzz(n_per: int = 3, seed: int = 20261028):
    rng = random.Random(seed)
    total, missed = 0, []
    for case in SEEDS:
        for name in TRANSFORMS:
            for _ in range(n_per):
                fenced = "```" in case["edits"][0].get("text", "")
                if fenced and name in WRAPS:
                    continue                                # 펜스를 쓰는 공격을 다시 감싸면 안팎이 뒤집혀 공격이 아니게 된다
                v = _variant(case, name, rng)
                ok_rules = set(case["expect"])
                if any(r.startswith("SAFETY") for r in ok_rules):
                    ok_rules |= {"SAFETY-DELETE", "SAFETY-WEAKEN"}   # 같은 safety 검사 안의 다른 규칙은 같은 이유
                if v["edits"][0]["op"] != "set" and (name == "heading" or fenced):
                    ok_rules |= {"SCOPE-ESCAPE"}            # 제목 삽입 · 깨진 코드 펜스는 섹션 탈출로 먼저 막힌다
                try:
                    d = run(v)
                    blocked = bool(d.rules & ok_rules)
                except Exception as x:                      # 형식 오류도 '막힘'이 아니라 기록한다
                    blocked, d = False, x
                total += 1
                if not blocked:
                    missed.append((case["id"], name, getattr(d, "rules", repr(d))))
    return total, missed


def test_fuzz_variants_blocked():
    total, missed = fuzz()
    assert total >= 1500
    assert not missed, f"{len(missed)}/{total} 변형 통과: {missed[:10]}"
