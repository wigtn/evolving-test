"""시험 문제 생성 (② Creator 단계, docs/16 6장).

- 생성 LLM은 finding(패턴 · 서명 · hint · 자산)만 본다. Creator의 가설과 Improver의 후보는 보지 않는다.
  그래야 후보가 통과하기 쉬운 문제를 만들 수 없다.
- 생성 LLM이 정하는 것: 어떤 업무 유형의 질문을 몇 개 만들지.
- 실험장에서는 질문 문장과 채점 기준을 가짜 ERP 세계의 케이스 생성기(gen/build.py)로 만든다.
  정답을 세계 데이터에서 계산해야 실험장의 규칙 채점이 돌기 때문이다.
  실제 시스템에서는 생성 LLM이 질문 문장을 쓰고, ③은 정답지 없이 규칙 검사 + 쌍대 비교로 판정한다.
- 생성한 질문이 정말 같은 결함을 건드리는지는 하네스(testset.select)가 지금 버전으로 돌려서 거른다.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import random

from . import registry, tester
from .config import DATA, RUNS

GEN_FILE = RUNS / "generated_cases.jsonl"
N_SCOPE, N_OTHER = 8, 2          # 결함 범위 유형당 / 범위 밖 유형당 생성 수 (범위 밖은 회귀 확인용 정상 문제)

PLAN_SYS = """너는 SEL 시험 문제 생성기다. finding(실패 패턴)만 보고, 그 패턴을 다시 일으킬 만한 사용자 질문의 업무 유형을 고른다.
고칠 후보나 원인 가설은 모른다. 아래 업무 유형 목록에서만 고른다.
답은 JSON 하나: {"kinds": ["업무 유형", ...], "reason": "한 문장"}"""


def _builder():
    spec = importlib.util.spec_from_file_location("sel_lab_case_builder", DATA.parent / "gen" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _world(b):
    raw = {k: [json.loads(x) for x in (DATA / "world" / f"{k}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
           for k in ("company_codes", "gl_accounts", "vendors", "documents", "gl_balances")}
    return b.World(raw)


def plan_kinds(finding: dict, kinds_map: dict, log=print) -> list[str]:
    """생성 LLM 자리: finding만 보고 질문 유형을 정한다. 오프라인이면 finding의 자산으로 정하는 규칙 대역."""
    from .config import offline
    from .testset import scope_kinds
    rule = sorted(scope_kinds(finding.get("asset_ref"), kinds_map))
    if offline():
        return rule
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from .config import chat_model
        from .sel import _text
        view = {k: finding.get(k) for k in ("asset_ref", "pattern", "signature", "hint")}
        msg = json.dumps({"finding": view, "kinds": {k: d for k, (_, d) in kinds_map.items()}}, ensure_ascii=False)
        txt = _text(chat_model("testgen").invoke([SystemMessage(PLAN_SYS), HumanMessage(msg)]))
        kinds = [k for k in json.loads(txt[txt.index("{"):txt.rindex("}") + 1]).get("kinds", []) if k in kinds_map]
        return sorted(set(kinds)) or rule
    except Exception as e:                                   # 생성 실패는 규칙으로 대신한다 (기록은 남긴다)
        log(f"  testgen: LLM 계획 실패, 규칙으로 대신 ({e.__class__.__name__})")
        return rule


def generate(finding: dict, *, seed: str, kinds_map: dict, log=print) -> list[str]:
    """finding에 맞는 질문을 새로 만들어 등록하고 ID 목록을 돌려준다. 필수 · 정책 케이스는 만들지 않는다(사람이 만든 고정 세트)."""
    scope = plan_kinds(finding, kinds_map, log=log)
    b = _builder()
    rnd = random.Random(int(hashlib.sha1(seed.encode()).hexdigest()[:8], 16))
    made = [c for c in b.build_cases(_world(b), rnd) if not c["fixed"]]
    tag = hashlib.sha1(seed.encode()).hexdigest()[:6]
    want = {k: (N_SCOPE if k in scope else N_OTHER) for k in kinds_map}
    out = []
    for c in made:
        if want.get(c["kind"], 0) <= 0:
            continue
        want[c["kind"]] -= 1
        out.append(c | {"eval_case_id": f"gen/{tag}/{c['eval_case_id']}", "source": "generated",
                        "pool": "generated", "covers_defects": []})
    tester.add_cases(out)
    GEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with GEN_FILE.open("a", encoding="utf-8") as f:
        for c in out:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    log(f"  testgen: 범위 {scope} → 질문 {len(out)}건 생성")
    return [c["eval_case_id"] for c in out]
