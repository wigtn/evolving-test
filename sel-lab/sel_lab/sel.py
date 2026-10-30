"""SEL 서비스: 개선 케이스 상태머신과 Creator·Improver·Tester·Evaluator·Gate·Promoter·Verifier.

케이스 = 자산 하나 · 패턴 하나 · 가설 하나. 자산당 활성 케이스는 하나. 상태 전이는 case_event에 추가 기록한다.
저장소는 로컬 SQLite (운영에서는 PostgreSQL sel 스키마를 SEL이 직접 읽고 쓴다. DBC는 업무 데이터를 건드릴 때만).
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

import yaml

from . import gate, registry, tester
from .config import DATA, RUNS, chat_model, offline

DB = RUNS / "sel.sqlite"
TERMINAL = {"VERIFIED", "REJECTED", "DISMISSED", "ROLLED_BACK", "ROLLBACK_SKIPPED"}
JUDGE_BUDGET = 3          # finding(dedupe_key)당 게이트 판정 총 횟수
HELD_RETRIES = 2          # 일시적 HELD 재시도 상한
CHANGE_LIMITS = {"instruction": 30}
GOVERNANCE_WRITES = 3     # 거버넌스 검사 실패 시 Improver 작성 최대 횟수 (docs/07 E)


# ---------- 저장소 ----------

def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
    create table if not exists finding(finding_id text primary key, dedupe_key text, case_id text, body text, received_at text);
    create table if not exists improvement_case(case_id text primary key, target_asset text, base_version text, dedupe_key text,
        state text, iteration int, fidelity text, signal text, body text, updated_at text);
    create table if not exists case_event(event_id text primary key, case_id text, from_state text, to_state text, actor text, payload text, at text);
    create table if not exists exposure(agent text, content_hash text, source text, at text, primary key(agent, content_hash));
    create table if not exists budget(dedupe_key text primary key, used int);
    create table if not exists suppressed(dedupe_key text primary key, reason text, at text);
    create table if not exists testset(id text primary key, sel_case_id text, iteration int, frozen_at text, status text, items text);
    create table if not exists case_usage(eval_case_id text primary key, uses int);
    """)
    return c


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _case(c, cid):
    r = c.execute("select * from improvement_case where case_id=?", (cid,)).fetchone()
    return {**dict(r), "body": json.loads(r["body"])} if r else None


def _save(c, case):
    c.execute("update improvement_case set state=?, iteration=?, body=?, updated_at=? where case_id=?",
              (case["state"], case["iteration"], json.dumps(case["body"], ensure_ascii=False), _now(), case["case_id"]))


def transition(c, case, to, actor, payload=None):
    c.execute("insert into case_event values(?,?,?,?,?,?,?)", (uuid.uuid4().hex, case["case_id"], case["state"], to, actor,
                                                               json.dumps(payload or {}, ensure_ascii=False), _now()))
    case["state"] = to
    _save(c, case)
    c.commit()


def _hash(text: str) -> str:
    import hashlib
    return hashlib.sha1(" ".join((text or "").split()).encode()).hexdigest()[:16]


def expose(agent: str, texts, source: str) -> None:
    """노출 원장: Creator 컨텍스트에 들어간 입력을 기록한다. 같은 입력의 골든셋 사례는 held-out이 될 수 없다.
    예전 문제은행 방식의 잔재다. 지금 설계는 시험 문제를 finding마다 새로 만들고 Creator는 사용자 질문을 보지 않아
    노출 원장이 없다(GEPA를 켤 때만 필요). 루프는 이 함수를 부르지 않는다(테스트에서만 쓰임)."""
    c = db()
    hashes = set()
    for x in texts:
        c.execute("insert or ignore into exposure values(?,?,?,?)", (agent, _hash(x), source, _now()))
        hashes.add(_hash(x))
    c.commit()
    from .testset import invalidate_exposed
    invalidate_exposed(c, hashes)          # 고정 뒤 노출: 판정 전 세트는 무효 → 판정 직전에 다시 고정


def exposed(agent: str) -> set[str]:
    return {r[0] for r in db().execute("select content_hash from exposure where agent=?", (agent,))}


def suppress(c, dedupe_key: str, reason: str) -> None:
    c.execute("insert or replace into suppressed values(?,?,?)", (dedupe_key, reason, _now()))
    c.commit()


def budget_used(c, dedupe_key: str) -> int:
    r = c.execute("select used from budget where dedupe_key=?", (dedupe_key,)).fetchone()
    return r[0] if r else 0


def ingest(findings: list[dict]) -> list[str]:
    """중복 키로 묶고, 활성 케이스가 있으면 증거로 붙이고, 없으면 케이스를 연다.
    suppressed finding(판정 예산 소진, REJECTED, 롤백)은 다시 열지 않는다."""
    c = db()
    opened = []
    sup = {r[0] for r in c.execute("select dedupe_key from suppressed")}
    for f in findings:
        if f["dedupe_key"] in sup:
            continue
        target = f["asset_ref"] or f"new/{((f.get('_lab') or {}).get('new_asset') or {}).get('procedure')}"
        base = target.split("@")[0]
        live = c.execute("select case_id from improvement_case where (dedupe_key=? or target_asset=?) and state not in (%s)"
                         % ",".join("?" * len(TERMINAL)), (f["dedupe_key"], base, *TERMINAL)).fetchone()
        if live:
            c.execute("insert or ignore into finding values(?,?,?,?,?)", (f["finding_id"], f["dedupe_key"], live[0], json.dumps(f, ensure_ascii=False), _now()))
            continue
        # 결정적 케이스 ID: 같은 finding 키의 몇 번째 케이스인지로 정한다 (시험 문제 분할 시드 · 후보 버전이 이 ID에서 나오므로)
        import hashlib
        nth = c.execute("select count(*) from improvement_case where dedupe_key=?", (f["dedupe_key"],)).fetchone()[0]
        cid = "case_" + hashlib.sha1(f"{f['dedupe_key']}|{nth}".encode()).hexdigest()[:8]
        body = {"finding": f, "history": []}
        c.execute("insert into improvement_case values(?,?,?,?,?,?,?,?,?,?)",
                  (cid, base, target.split("@")[1] if "@" in target else None, f["dedupe_key"], "OPEN", 0,
                   f["_lab"]["fidelity"], f["pattern"], json.dumps(body, ensure_ascii=False), _now()))
        c.execute("insert into finding values(?,?,?,?,?)", (f["finding_id"], f["dedupe_key"], cid, json.dumps(f, ensure_ascii=False), _now()))
        c.execute("insert into case_event values(?,?,?,?,?,?,?)", (uuid.uuid4().hex, cid, None, "OPEN", "analyzer", "{}", _now()))
        opened.append(cid)
    c.commit()
    return opened


# ---------- LLM 호출 공통 ----------

def _text(out) -> str:
    c = out.content if isinstance(out.content, str) else "".join(
        x.get("text", "") if isinstance(x, dict) else str(x) for x in out.content)
    return re.sub(r"<think>.*?</think>", "", c, flags=re.S)       # 추론 모델의 사고 블록 제거


def _json_from(text: str) -> dict:
    """모델이 코드블록·설명을 섞어도 첫 JSON 객체를 꺼낸다 (provider 전용 structured output에 기대지 않는다).
    문자열 안의 줄바꿈도 허용한다(strict=False)."""
    dec = json.JSONDecoder(strict=False)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text, i)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    raise ValueError("JSON 없음: " + text[:200])


def _ask(role: str, system: str, user: str, retries: int = 1) -> dict:
    from langchain_core.messages import HumanMessage, SystemMessage
    last = None
    for _ in range(retries + 1):
        out = chat_model(role).invoke([SystemMessage(system), HumanMessage(user)])
        try:
            return _json_from(_text(out))
        except ValueError as e:
            last = e
            user += "\n\n직전 답에서 JSON을 읽지 못했다. JSON 객체 하나만 답하라."
    raise last


def _evidence(finding, limit=5):
    """증거는 finding에 실려 오지 않는다. ①의 정규화 기록(sel_run)을 ②가 직접 읽는다(lab: analyzer.failing_runs)."""
    from . import tracing
    from .analyzer import failing_runs
    fails = failing_runs(finding)
    ids = {e["run_id"] for e in fails}
    if finding["pattern"] == "eval_fail":                   # lab 전용 eval_fail: 고정 사례의 질문 문장은 Creator에 넘기지 않는다
        return [{"case_id": e["case_id"], "case_kind": e["case_id"].split("/")[0],
                 "failed_checks": e.get("failed_checks", [])} for e in fails[:limit]]
    runs = [r for r in tracing.read("runs_prod") + tracing.read("runs_eval") if r["run_id"] in ids][:limit]
    # 사용자 질문 · 도구 인자 값 · 답변은 넘기지 않고 구조만 넘긴다
    # (① sel_run이 남기는 것과 같은 범위: 도구 이름 · 인자 이름 · 결과 코드 · 결과 건수 · 세션 이벤트 종류)
    events = {e["run_id"]: [x["type"] for x in e.get("events", [])] for e in fails}
    out = []
    for r in runs:
        calls = []
        for c in r["tool_calls"][:12]:
            res = c.get("result") if isinstance(c.get("result"), dict) else {}
            calls.append({"name": c["name"], "arg_names": sorted(c["args"]),
                          "result_code": res.get("error") or "OK",
                          "rows": len(res["rows"]) if isinstance(res.get("rows"), list) else None})
        out.append({"run_id": r["run_id"], "status": r["status"], "turns": r.get("turns"),
                    "tool_calls": calls, "session_events": events.get(r["run_id"], [])})
    return out


def _assets_view():
    b = registry.resolve_agent()
    return {a.ref: a.text for a in [b["agent"], *b["prompts"], *b["skills"], *b["tools"]]}


KINDS = {
    "open_items": ("asset://skill/fi/open-items", "기간을 지정한 미결 조회"),
    "open_items_no_period": ("asset://skill/fi/open-items", "기간 없이 미결 조회 (당월 기본값)"),
    "open_items_annual": ("asset://skill/fi/open-items", "연간·누적·전체 기간 미결 조회"),
    "open_items_ambiguous": ("asset://skill/fi/open-items", "이름이 겹치는 거래처 (되묻기)"),
    "open_items_multi_currency": ("asset://skill/fi/open-items", "통화가 섞인 거래처"),
    "gl_balance": ("asset://skill/fi/gl-balance", "계정 잔액 (월 표현 변환)"),
    "document_post": ("asset://skill/fi/document-post", "전표 전기 (dry_run·승인)"),
    "multi_vendor": ("asset://agent/fi/assistant", "여러 거래처 한 번에 (다단계)"),
}


# ---------- Creator ----------

CREATOR_SYS = """너는 SEL Creator다. 실패 패턴(finding)과 증거 실행 기록, 현행 자산을 보고 가설을 세운다.
고칠 수 있는 범위: Agent는 spec.params의 허용 필드만(composition), SKILL 본문(instruction, example), Prompt는 persona만(instruction),
Tool은 description·인자 설명·pattern(metadata). judge 프롬프트와 평가 케이스는 고칠 수 없다.
원인이 도구 구현·플랫폼 장애·데이터·권한이면 dismiss한다. 증거가 부족해도 dismiss한다.
JSON 하나로만 답한다:
{"decision":"proceed|dismiss","reason":"...","target_asset_ref":"asset://종류/ns/이름@버전","change_type":"instruction|metadata|composition|example|new_asset",
 "field_path":"...","hypothesis":"한 문장","confidence":0.0~1.0,"target_kinds":["..."],"guard_kinds":["..."]}"""


def creator(case) -> dict:
    f = case["body"]["finding"]
    ev = _evidence(f)
    prev = case["body"]["history"][-1] if case["body"]["history"] else None
    if offline():
        return _creator_offline(f, ev, prev)
    user = json.dumps({"finding": {k: f[k] for k in ("pattern", "asset_ref", "hint", "metrics")}, "evidence": ev,
                       "assets": _assets_view(), "case_kinds": {k: v[1] for k, v in KINDS.items()},
                       "previous_iteration": prev}, ensure_ascii=False)
    return _ask("creator", CREATOR_SYS, user)


def kind_from_structure(e: dict) -> str | None:
    """오프라인 대역용: 실행의 구조(어떤 도구를 어떤 인자 이름으로, 결과가 몇 건)만 보고 업무 유형을 추정한다.
    사용자 질문은 보지 않는다."""
    if e.get("case_kind"):                                   # 골든셋 사례는 평가 케이스 라벨이 있다
        return e["case_kind"]
    calls = e.get("tool_calls", [])
    names = [c["name"] for c in calls]
    if "fi_document_post" in names:
        return "document_post"
    if "fi_gl_balance_query" in names:
        return "gl_balance"
    if names.count("fi_vendor_search") >= 2:
        return "multi_vendor"
    if any(c["name"] == "fi_vendor_search" and (c.get("rows") or 0) >= 2 for c in calls):
        return "open_items_ambiguous"
    if any(c["name"] == "fi_open_items_query" and "period_from" not in c["arg_names"] for c in calls):
        return "open_items_no_period"
    if "fi_open_items_query" in names:
        return "open_items"
    return None


def _creator_offline(f, ev, prev):
    """LLM 없이 배관을 검증하는 규칙 대역. 증거 실행의 구조(도구 · 인자 이름 · 결과 건수)와 세션 이벤트 종류로
    대상을 고른다 (사용자 질문 · 정답지는 보지 않는다)."""
    sig = f["pattern"]
    code = (f.get("signature") or {}).get("result_code")
    kinds = sorted({k for k in (kind_from_structure(e) for e in ev) if k})
    tgt = f["asset_ref"]
    if sig == "no_asset_match":
        return {"decision": "dismiss", "reason": "offline 모드는 new_asset을 만들지 않는다"}
    if sig == "tool_arg_error" and code == "ARG_INVALID":      # 형식 위반 → 도구 설명
        return {"decision": "proceed", "target_asset_ref": tgt, "change_type": "metadata", "field_path": "spec.description",
                "hypothesis": "도구 설명에 인자 형식이 없어 잘못된 형식으로 호출한다", "confidence": 0.6,
                "target_kinds": kinds or ["gl_balance"], "guard_kinds": []}
    if sig == "tool_arg_error":                                # 업무 오류(ROW_LIMIT 등) → 절차(SKILL)
        base = tgt.split("@")[0]
        skill = next(s for s in registry.resolve_agent()["skills"] if any(t.startswith(base) for t in s.meta.get("allowed_tools", [])))
        guard = [k for k, (a, _) in KINDS.items() if a == skill.base and k not in kinds][:2]
        return {"decision": "proceed", "target_asset_ref": skill.ref, "change_type": "instruction", "field_path": "body",
                "hypothesis": "SKILL 절차에 인자 결정 규칙이 빠져 도구 오류가 난다", "confidence": 0.5,
                "target_kinds": kinds, "guard_kinds": guard}
    if sig == "eval_fail":
        b = KINDS.get(kinds[0])[0] if kinds else None
        a = next((x for x in [*registry.resolve_agent()["skills"], *registry.resolve_agent()["prompts"]] if x.base == b), None)
        if not a:
            return {"decision": "dismiss", "reason": "실패 사례의 자산을 특정하지 못함"}
        return {"decision": "proceed", "target_asset_ref": a.ref, "change_type": "instruction", "field_path": "body",
                "hypothesis": "고정 세트 실패 유형의 SKILL 절차에 필수 단계가 빠졌다", "confidence": 0.5,
                "target_kinds": kinds, "guard_kinds": []}
    if sig == "limit_exceeded":
        return {"decision": "proceed", "target_asset_ref": tgt, "change_type": "composition", "field_path": "spec.params.max_turns",
                "hypothesis": "턴 상한이 다단계 업무에 부족하다", "confidence": 0.6, "target_kinds": kinds or ["multi_vendor"], "guard_kinds": []}
    # repeat_question: 세션 이벤트로 대상 결정
    # 이벤트 종류(재요청 · 정정)와 실행 구조만 본다. 발화 내용은 보지 않는다
    evs = [t for e in ev for t in e.get("session_events", [])]
    fmt = sum(1 for t in evs if t == "correction")
    ev_kinds = kinds
    ambiguous = sum(1 for e in ev if kind_from_structure(e) == "open_items_ambiguous")
    if ev and ambiguous * 2 >= len(ev):           # 거래처 검색이 2건 이상인데 묻지 않고 조회한 실행이 대부분
        s = registry.resolve_agent()["skills"][0]
        return {"decision": "proceed", "target_asset_ref": s.ref, "change_type": "instruction", "field_path": "body",
                "hypothesis": "이름이 겹치는 거래처를 묻지 않고 하나를 골라 재요청이 생긴다", "confidence": 0.5,
                "target_kinds": ["open_items_ambiguous"], "guard_kinds": ["open_items"]}
    if fmt * 2 >= len(evs):
        p = registry.resolve_agent()["prompts"][0]
        return {"decision": "proceed", "target_asset_ref": p.ref, "change_type": "instruction", "field_path": "body",
                "hypothesis": "persona에 금액 표기 규칙이 없어 사용자가 고쳐 쓴다", "confidence": 0.5,
                "target_kinds": ev_kinds or ["open_items"], "guard_kinds": []}
    s = registry.resolve_agent()["skills"][0]
    return {"decision": "proceed", "target_asset_ref": s.ref, "change_type": "instruction", "field_path": "body",
            "hypothesis": "모호한 대상을 묻지 않고 진행해 재요청이 생긴다", "confidence": 0.4,
            "target_kinds": kinds or ["open_items_ambiguous"], "guard_kinds": ["open_items"]}


def _informative(c) -> bool:
    """정답에 값이 있는 사례 (예: '미결 없음' 사례는 금액 표기 차이를 드러내지 못한다)."""
    a = c["expected"].get("answer")
    return not isinstance(a, dict) or bool(a) or "answer_by_vendor" in c["expected"]


def _validate_hypothesis(h: dict) -> dict:
    """Creator 가설 검증. 대상은 반드시 현재 활성 버전이다 (모델이 버전을 틀리게 적어 다른 버전이 입력되는 것을 막는다)."""
    ref = h.get("target_asset_ref") or ""
    if h.get("change_type") == "new_asset":
        return {"decision": "dismiss", "reason": "new_asset 후보 생성은 아직 구현하지 않았다"}
    base = ref.split("@")[0]
    if base not in registry.read_state()["active"]:
        return {"decision": "dismiss", "reason": f"알 수 없는 대상 자산: {ref}"}
    h["target_asset_ref"] = registry.active_ref(base)
    kind = registry.kind_of(base)
    if h.get("change_type") not in ALLOWED_CHANGE.get(kind, set()):
        return {"decision": "dismiss", "reason": f"{kind}에 허용되지 않는 변경 유형: {h.get('change_type')}"}
    h["target_kinds"] = [k for k in h.get("target_kinds") or [] if k in KINDS]
    h["guard_kinds"] = [k for k in h.get("guard_kinds") or [] if k in KINDS and k not in h["target_kinds"]]
    if not h["target_kinds"]:
        return {"decision": "dismiss", "reason": "목표 업무 유형이 비었거나 알 수 없음"}
    return h


DEV_PER_KIND = 2   # dev 쪽 비대칭 분할: 업무 유형당 소량만 (PRD: 최대 10)


def split_golden(seed: str, agent: str = registry.DEFAULT_AGENT) -> set[str]:
    """예전 문제은행 방식의 잔재(루프는 부르지 않고 테스트에서만 쓴다). 지금은 시험 문제를 testgen이 새로 만든다.
    held-out = 노출 원장 밖의 골든셋 사례 중 dev 소량을 뺀 나머지.
    Creator 컨텍스트에 한 번이라도 들어간 입력의 사례는 시드와 무관하게 held-out에 들어오지 않는다.
    lab 데이터에는 template_id가 없어 사례 하나를 템플릿 하나로 본다."""
    import zlib
    seen = exposed(agent)
    pool = [c for c in tester.cases().values() if not c["fixed"] and _hash(c["input"]["user_message"]) not in seen]
    by_kind = {}
    for c in sorted(pool, key=lambda c: zlib.crc32(f"{seed}|{c['eval_case_id']}".encode())):
        by_kind.setdefault(c["kind"], []).append(c["eval_case_id"])
    return {cid for ids in by_kind.values() for cid in ids[DEV_PER_KIND:]}


def build_eval_set(finding: dict | None, target_ref: str | None = None, n_target=12, n_guard=8, n_probe=200,
                   seed: str = "default", log=print, runner=None) -> dict:
    """시험 문제 생성 + 하네스 (docs/16 6장).
    생성기(testgen)는 finding만 보고 질문을 새로 만든다. Creator의 가설 · 후보는 보지 않는다.
    하네스(testset.select)는 그 질문을 지금 버전으로 돌려, 같은 서명으로 틀리는 것(목표)과 정상인 것만 남긴다.
    필수 · 정책 케이스는 사람이 만든 고정 세트에서 넣는다."""
    from . import testgen, testset
    pool = set(testgen.generate(finding or {}, seed=seed, kinds_map=KINDS, log=log))
    es = testset.select(pool, target_ref=None, finding=finding, kinds_map=KINDS,
                        usage=testset.usage_map(db()), seed=seed, n_target=n_target, n_guard=n_guard,
                        n_probe=n_probe, runner=runner, log=log)
    build_eval_set.last_stats = es.pop("_stats", None)
    return es


def _freeze_set(c, case, h, log, refreeze: bool = False) -> None:
    from . import testset
    seed = f"{case['case_id']}|{case['iteration']}" + ("|refreeze" if refreeze else "")
    h["eval_set"] = build_eval_set(case["body"]["finding"], target_ref=h.get("target_asset_ref"), seed=seed, log=log)
    h["test_set"] = testset.freeze(c, case["case_id"], case["iteration"], h["eval_set"])
    h["test_set"]["stats"] = getattr(build_eval_set, "last_stats", None)
    if refreeze:
        h["test_set"]["refrozen"] = True


# ---------- Improver ----------

IMPROVER_SYS = """너는 SEL Improver다. 가설과 현행 자산 원문을 받아, 변경 유형 제약 안에서 고친 자산 원문 전체를 돌려준다.
- instruction: 본문만, 변경 30줄 이내, 섹션 단위. front matter의 allowed_tools는 바꾸지 않는다
- metadata: tool의 spec.description, 인자 description, pattern만
- composition: agent의 spec.params 허용 필드만
- example: 예시 블록만. 예시 값은 '가상' 합성 값만
metadata.ref 줄과 front matter 구조는 그대로 둔다 (버전은 시스템이 붙인다).
답 형식: 먼저 한 줄로 변경 요지를 쓰고, 고친 원문 전체를 아래 구분자 사이에 그대로 넣는다. 코드 펜스를 쓰지 않는다.
<<<ASSET
(고친 자산 원문 전체)
ASSET>>>"""


def improver(case, h) -> dict:
    base_ref = h["target_asset_ref"]
    src = registry.load(base_ref)
    if offline():
        return _improver_oracle(src, h.get("change_type", "instruction"))
    from langchain_core.messages import HumanMessage, SystemMessage
    hyp = {k: h.get(k) for k in ("target_asset_ref", "change_type", "field_path", "hypothesis")}   # 판정 세트는 넘기지 않는다
    fb = case["body"].get("governance_feedback")       # 직전 거버넌스 실패 범주만 (W2-11)
    msg = json.dumps({"hypothesis": hyp, "asset_text": src.text} | ({"governance_failed": fb} if fb else {}), ensure_ascii=False)
    for _ in range(2):
        txt = _text(chat_model("improver").invoke([SystemMessage(IMPROVER_SYS), HumanMessage(msg)]))
        m = re.search(r"<<<ASSET\n(.*?)\nASSET>>>", txt, re.S)
        if m:
            body = re.sub(r"^```\w*\n|\n```$", "", m.group(1).strip("\n"))
            return {"text": body + ("\n" if not body.endswith("\n") else ""), "note": txt[:m.start()].strip()[:300]}
        msg += "\n\n직전 답에 <<<ASSET ... ASSET>>> 블록이 없었다. 형식을 지켜라."
    raise ValueError("Improver 출력에 자산 블록이 없음")


CHANGE_KIND = {"tool": "tool_description", "agent": "manifest_param", "prompt": "prompt_instruction"}


def _change_kind(src, change_type: str) -> str:
    if src.kind == "skill":
        return "example" if change_type == "example" else "skill_body"
    return CHANGE_KIND[src.kind]


def _leaves(d, prefix=()):
    if isinstance(d, dict):
        for k, v in d.items():
            yield from _leaves(v, prefix + (k,))
    else:
        yield prefix, d


def _improver_oracle(src, change_type: str = "instruction") -> dict:
    """ORACLE: 정상 버전과 다른 곳을 편집 명령으로 되돌린다 (W1). 허용 여부는 거르지 않는다(거버넌스 검사기 W2가 판정).
    배관 검증 전용이며 개선 능력을 재지 않는다(원인 적중률이 구조상 100%)."""
    from .edit.address import Address
    from .edit.sections import all_paths, block
    healthy = registry.parse(registry.healthy_path(src.kind, src.ns, src.name).read_text(encoding="utf-8"),
                             registry.healthy_path(src.kind, src.ns, src.name))
    edits = []
    if src.path.suffix == ".md":
        a_lines, b_lines = src.body.split("\n"), healthy.body.split("\n")
        for path in all_paths(a_lines):
            if path not in all_paths(b_lines):
                continue
            s1, e1 = block(a_lines, path)
            s2, e2 = block(b_lines, path)
            if "\n".join(a_lines[s1:e1]).strip() != "\n".join(b_lines[s2:e2]).strip():
                addr = Address(src.ref, "section", section=path)
                edits.append({"op": "replace_section", "path": str(addr),
                              "text": "\n".join(b_lines[s2:e2]).strip("\n")})
    else:
        old = dict(_leaves(src.meta))
        for path, val in _leaves(healthy.meta):
            if path in old and old[path] != val and type(old[path]) is type(val) and path != ("metadata", "ref"):
                addr = Address(src.ref, "field", field=path)
                edits.append({"op": "set", "path": str(addr), "value": val})
    if not edits:
        raise ValueError("ORACLE: 되돌릴 차이가 없다")
    bundle = {"base_ref": src.ref, "change_kind": _change_kind(src, change_type), "edits": edits}
    return {"edits": bundle, "note": "ORACLE(offline): 정상 버전과 다른 곳을 편집 명령으로 복원"}


ALLOWED_CHANGE = {"skill": {"instruction", "example"}, "prompt": {"instruction", "example"},
                  "tool": {"metadata"}, "agent": {"composition"}}


def _tool_meta_paths(d, prefix=""):
    """tool YAML의 값 경로 목록 (metadata 변경 화이트리스트 검사용)."""
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_tool_meta_paths(v, f"{prefix}.{k}" if prefix else k))
    else:
        out[prefix] = json.dumps(d, ensure_ascii=False, sort_keys=True)
    return out


def static_check(src, new_text: str, change_type: str) -> list[str]:
    errs = []
    try:
        registry.parse(new_text, src.path)
    except Exception as e:
        return [f"자산 파싱 실패: {repr(e)[:120]}"]
    if change_type not in ALLOWED_CHANGE.get(src.kind, set()):
        errs.append(f"{src.kind}에 허용되지 않는 변경 유형: {change_type}")
    if src.kind == "tool":
        old, new = _tool_meta_paths(yaml.safe_load(src.text)), _tool_meta_paths(yaml.safe_load(new_text))
        bad = [p for p in set(old) | set(new) if old.get(p) != new.get(p) and p != "metadata.ref"
               and not (p == "spec.description" or (p.startswith("spec.input_schema.") and p.endswith((".description", ".pattern"))))]
        if bad:
            errs.append(f"tool metadata 밖 변경: {sorted(bad)[:5]}")
    if src.kind == "prompt" and src.meta.get("role") == "judge":
        errs.append("judge 프롬프트는 수정 불가")
    if src.kind == "tool" and change_type != "metadata":
        errs.append("tool은 metadata 변경만 허용")
    a = re.sub(r"\bref:\s*\S+", "", src.text).splitlines()
    b = re.sub(r"\bref:\s*\S+", "", new_text).splitlines()
    changed = [l for l in difflib.unified_diff(a, b, lineterm="", n=0) if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    if not changed:
        errs.append("변경 없음")
    if change_type == "instruction" and len(changed) > CHANGE_LIMITS["instruction"] * 2:
        errs.append(f"instruction 변경 한도 초과 ({len(changed)}줄)")
    if src.kind == "agent":
        old, new = _tool_meta_paths(yaml.safe_load(src.text)), _tool_meta_paths(yaml.safe_load(new_text))
        from .governance import default as _policy
        editable = {f"spec.params.{x}" for x in _policy()["allow"]["agent"]["params"]}   # W2 정책 파일 (기본 빈 목록)
        bad = [p for p in set(old) | set(new) if old.get(p) != new.get(p) and p != "metadata.ref" and p not in editable]
        if bad:
            errs.append(f"agent 허용 필드 밖 변경: {sorted(bad)[:5]}")
    if src.path.suffix == ".md":
        def fm(t):
            d = yaml.safe_load(re.match(r"^---\n(.*?)\n---", t, re.S).group(1))
            d.get("metadata", {}).pop("ref", None)
            return d
        if fm(src.text) != fm(new_text):
            errs.append("front matter 변경 불가 (allowed_tools, kind, role 등)")
    if src.kind == "prompt" and src.meta.get("role") != "persona":
        errs.append("prompt는 persona만 수정 가능")
    return errs


def _bump(ver: str, tag: str) -> str:
    core = ver.split("-")[0]
    x, y, z = (core.split(".") + ["0", "0"])[:3]
    return f"{x}.{y}.{int(z) + 1}-{tag}"


def _stale_evidence(finding) -> list[str]:
    """증거 실행이 쓴 자산 중, finding 대상 자산이나 그 대상이 의존하는 SKILL·persona가 이후 교체됐는가.
    무관한 자산의 교체로 열린 케이스를 닫지 않는다."""
    from . import tracing
    from .analyzer import failing_runs
    ids = {e["run_id"] for e in failing_runs(finding)}
    tgt = (finding.get("asset_ref") or "").split("@")[0]
    if not tgt:
        return []
    watch = {tgt}
    b = registry.resolve_agent()
    if registry.kind_of(tgt) == "tool":                    # 도구 신호의 원인은 SKILL·persona에 있을 수 있다
        watch |= {a.base for a in b["skills"] + b["prompts"]}
    elif registry.kind_of(tgt) == "agent":                 # 에이전트 단위 신호(재요청·예산)는 묶음의 어느 자산에서든 올 수 있다
        watch |= {a.base for a in b["skills"] + b["prompts"] + b["tools"]}
    refs = {r for x in tracing.read("runs_prod") + tracing.read("runs_eval") if x["run_id"] in ids for r in x["asset_refs"]}
    active = registry.read_state()["active"]
    return sorted(r for r in refs if r.split("@")[0] in watch and active.get(r.split("@")[0]) != r.split("@")[1])


# ---------- 오케스트레이터 ----------

def step(cid: str, k: int = 3, log=print) -> str:
    c = db()
    case = _case(c, cid)
    st = case["state"]
    if st == "OPEN":
        stale = _stale_evidence(case["body"]["finding"])
        if stale:   # 증거가 이미 교체된 버전에서 나왔다 → 다음 분석에서 새 버전으로 재확인
            transition(c, case, "DISMISSED", "sel_service", {"reason": "증거 버전이 더 이상 활성 아님", "stale": stale})
            return "DISMISSED"
        try:
            h = creator(case)
            if h.get("decision") == "proceed":
                h = _validate_hypothesis(h)
        except Exception as e:      # 모델 출력이 깨져도 루프 전체를 죽이지 않는다
            h = {"decision": "dismiss", "reason": f"Creator 실패: {repr(e)[:200]}"}
        if h.get("decision") != "proceed":
            transition(c, case, "DISMISSED", "sel_creator", {"reason": h.get("reason")})
        else:
            case["iteration"] += 1
            case["body"].pop("governance_rejections", None)    # 거버넌스 재작성 횟수 · 범주는 가설마다 새로
            case["body"].pop("governance_feedback", None)
            _freeze_set(c, case, h, log)
            case["body"]["hypothesis"] = h
            transition(c, case, "TRIAGED", "sel_creator", {"hypothesis": h["hypothesis"], "target": h["target_asset_ref"]})
    elif st == "TRIAGED":
        h = case["body"]["hypothesis"]
        src = registry.load(h["target_asset_ref"])
        from . import governance
        fp = h.get("field_path")
        if src.path.suffix != ".md" and fp and not governance.target_allowed(src, f"{src.ref}:{fp}"):
            # 가설의 대상 주소부터 정책 밖: 생성하지 않고 끝낸다(다시 만들어도 바뀌지 않는다)
            pre = governance.reject_before_generation(src, f"{src.ref}:{fp}")
            governance.log_decision(pre, f"{cid}/{case['iteration']}")
            case["body"]["governance"] = {"policy_ref": pre.policy_ref, "policy_hash": pre.policy_hash, "ok": False,
                                          "checks": pre.checks, "rules": sorted(pre.rules), "stage": "before_generation"}
            transition(c, case, "REJECTED", "sel_improver", {"reason": "대상 주소가 거버넌스 정책 밖", "c6": "governance_rejected",
                                                             "address": f"{src.ref}:{fp}"})
            return "REJECTED"
        gov = None
        try:
            out = improver(case, h)
            if "edits" in out:                             # W1 편집 명령 → W2 검사기(정책 파일로 적용 · 내용 검사)
                from .edit import EditError
                try:
                    gov = governance.check(src, out["edits"], change_kind=_change_kind(src, h["change_type"]))
                except EditError as x:
                    raise ValueError(f"{x.code}: {x.detail}")
                governance.log_decision(gov, f"{cid}/{case['iteration']}")
                case["body"]["governance"] = {"policy_ref": gov.policy_ref, "policy_hash": gov.policy_hash, "ok": gov.ok,
                                              "checks": gov.checks, "rules": sorted(gov.rules)}
                if gov.ok:
                    case["body"].pop("governance_feedback", None)
                    out["text"] = gov.text
                    case["body"]["edits"] = {"bundle": out["edits"], "addresses": gov.edit.addresses,
                                             "changed_lines": gov.edit.changed_lines}
                    errs = []
                else:
                    errs = [f"governance:{x}" for x in governance.feedback(gov)]   # Improver에는 범주만
            else:
                errs = static_check(src, out["text"], h["change_type"])
        except Exception as e:
            out, errs = None, [f"Improver 실패: {repr(e)[:200]}"]
        if gov is not None and not gov.ok:                 # docs/07 E: 범주만 돌려받아 다시 쓴다. 작성 3번까지
            n = case["body"].get("governance_rejections", 0) + 1
            case["body"]["governance_rejections"] = n
            case["body"]["governance_feedback"] = governance.feedback(gov)
            if n >= GOVERNANCE_WRITES:
                transition(c, case, "REJECTED", "sel_improver", {"reason": "거버넌스 검사 반복 실패", "c6": "governance_rejected",
                                                                 "static_check": errs})
                return "REJECTED"
            transition(c, case, "TRIAGED", "sel_improver", {"governance_retry": n, "static_check": errs})
            return "TRIAGED"
        if errs:
            case["body"]["last_failure"] = {"iteration": case["iteration"], "static_check": errs}
            transition(c, case, "GATE_FAILED", "sel_improver", {"static_check": errs})
        else:
            ver = _bump(src.version, f"c{cid[-4:]}{case['iteration']}")
            cand = registry.write_candidate(src.ref, ver, out["text"])
            case["body"]["candidate"] = {"ref": cand.ref, "note": out["note"]}
            transition(c, case, "CANDIDATE_READY", "sel_improver", {"candidate": cand.ref})
    elif st == "CANDIDATE_READY":
        dk = case["dedupe_key"]
        if budget_used(c, dk) >= JUDGE_BUDGET:
            suppress(c, dk, "판정 예산 소진")
            transition(c, case, "REJECTED", "sel_service", {"reason": "판정 예산 소진"})
            return "REJECTED"
        h = case["body"]["hypothesis"]
        from . import testset
        if testset.status(c, h["test_set"]["id"]) != "frozen":     # 고정 뒤 무효면 다시 고정 (노출 원장 잔재: 루프는 expose를 안 불러 지금은 안 일어남)
            _freeze_set(c, case, h, log, refreeze=True)
        transition(c, case, "EVALUATING", "tester")
        cand = case["body"]["candidate"]["ref"]
        base, ver = cand.split("@")
        results = tester.paired_run(h["eval_set"], {base: ver}, k=k, log=log)
        dec = gate.decide(results, case["fidelity"])
        testset.mark_judged(c, h["test_set"]["id"], h["eval_set"])
        case["body"]["gate"] = dec
        case["body"]["results"] = results
        if dec["verdict"] != "HELD":                       # HELD는 판정이 아니므로 예산을 쓰지 않는다
            c.execute("insert into budget values(?,1) on conflict(dedupe_key) do update set used=used+1", (dk,))
        if dec["verdict"] == "HELD" and dec.get("held_reason") == "structural":
            suppress(c, dk, "구조적 판정 불가: " + dec.get("held_detail", ""))
            transition(c, case, "REJECTED", "gate", {"reason": "구조적 판정 불가", "detail": dec.get("held_detail")})
            return "REJECTED"
        nxt = {"PASS": "GATE_PASSED", "FAIL": "GATE_FAILED", "HELD": "HELD"}[dec["verdict"]]
        transition(c, case, nxt, "gate", {k2: dec[k2] for k2 in ("G1", "G2", "G3", "G4")} | {"target": dec["target"],
                                                                                          "budget_used": budget_used(c, dk)})
    elif st == "HELD":                                     # 일시적 판정 불가: 같은 세트로 재시도
        n = case["body"].get("held_retries", 0)
        if n >= HELD_RETRIES:
            if case["body"].get("candidate"):              # 판정 못 한 후보도 남기지 않는다 → rejected
                registry.set_lifecycle(case["body"]["candidate"]["ref"], "rejected")
            transition(c, case, "REJECTED", "sel_service", {"reason": "HELD 재시도 소진"})
        else:
            case["body"]["held_retries"] = n + 1
            case["body"].pop("results", None)              # 이전 부분 결과는 버린다
            transition(c, case, "CANDIDATE_READY", "sel_service", {"retry": n + 1})
    elif st == "GATE_PASSED":
        registry.set_lifecycle(case["body"]["candidate"]["ref"], "candidate")
        transition(c, case, "AWAITING_APPROVAL", "sel_service")
    elif st == "GATE_FAILED":
        if case["body"].get("candidate"):                  # 평가 미달 → Registry rejected (기준 문서 그림 4)
            registry.set_lifecycle(case["body"]["candidate"]["ref"], "rejected")
        g = case["body"].pop("gate", None)
        case["body"]["history"].append({"iteration": case["iteration"],
                                        "hypothesis": {k: v for k, v in (case["body"].get("hypothesis") or {}).items() if k != "eval_set"},
                                        "static_check": (case["body"].pop("last_failure", None) or {}).get("static_check"),
                                        "gate": {k: g[k] for k in ("verdict", "G1", "G2", "G3", "G4", "target", "guard")} if g else None})
        case["body"].pop("candidate", None)
        case["body"].pop("results", None)
        if budget_used(c, case["dedupe_key"]) >= JUDGE_BUDGET:
            suppress(c, case["dedupe_key"], "판정 예산 소진")
            transition(c, case, "REJECTED", "sel_orchestrator", {"reason": "판정 예산 소진"})
        else:
            transition(c, case, "OPEN", "sel_orchestrator")
    return _case(c, cid)["state"]


def run_until_wait(cid: str, k: int = 3, log=print) -> str:
    for _ in range(20):
        st = _case(db(), cid)["state"]
        if st in TERMINAL | {"AWAITING_APPROVAL", "VERIFYING"}:
            return st
        log(f"[{cid}] {st} →")
        step(cid, k=k, log=log)
    return _case(db(), cid)["state"]


# ---------- 승인 · 릴리스 · 검증 ----------

def approve(cid: str, approver: str, comment: str = "") -> str:
    c = db()
    case = _case(c, cid)
    assert case["state"] == "AWAITING_APPROVAL", case["state"]
    transition(c, case, "APPROVED", approver, {"comment": comment})
    cand = case["body"]["candidate"]["ref"]
    prev = registry.set_active(cand, f"{cid} 승인", approver)   # full 충실도: 전체 릴리스
    case["body"]["release"] = {"version": cand, "prev": prev}
    transition(c, case, "RELEASED", "promoter", case["body"]["release"])
    transition(c, case, "VERIFYING", "promoter")
    return "VERIFYING"


def reject(cid: str, approver: str, reason: str) -> str:
    c = db()
    case = _case(c, cid)
    suppress(c, case["dedupe_key"], f"승인자 반려: {reason}")
    if case["body"].get("candidate"):                      # 반려 → Registry rejected
        registry.set_lifecycle(case["body"]["candidate"]["ref"], "rejected")
    transition(c, case, "REJECTED", approver, {"reason": reason})
    return "REJECTED"


def verify(cid: str, after: dict) -> str:
    """after: analyzer 결과(반영 후 배치). 같은 신호의 발생률이 기준 이하로 내려갔는지 본다."""
    c = db()
    case = _case(c, cid)
    f = case["body"]["finding"]
    before = f["metrics"]["fail_rate"]
    same = [x for x in after["findings"] + after["below_threshold"]
            if x["pattern"] == f["pattern"] and x["dedupe_key"] == f["dedupe_key"]]
    after_rate = max((x["metrics"]["fail_rate"] for x in same), default=0.0)
    ok = after_rate <= max(before / 2, 0.02)
    case["body"]["verification"] = {"rate_before": before, "rate_after": after_rate}
    transition(c, case, "VERIFIED" if ok else "REGRESSED", "verifier", case["body"]["verification"])
    if not ok:
        auto_rollback(c, case)
    return case["state"]


def auto_rollback(c, case) -> None:
    """직전 승인 버전으로 자동 롤백. CAS: 현재 활성 == 이 케이스가 릴리스한 버전일 때만."""
    rel = case["body"]["release"]
    base = rel["version"].split("@")[0]
    now = registry.active_ref(base)
    if now != rel["version"]:                              # 롤백하지 않고 기록만. finding은 known으로 두어 다음 분석이 현재 버전으로 다시 잡는다
        case["body"]["rollback"] = {"cas": False, "active": now, "released": rel["version"]}
        transition(c, case, "ROLLBACK_SKIPPED", "verifier", case["body"]["rollback"])
        return
    suppress(c, case["dedupe_key"], "반영 후 악화")
    registry.set_active(f"{base}@{rel['prev']}", f"{case['case_id']} 자동 롤백", "verifier")
    case["body"]["rollback"] = {"cas": True, "to": rel["prev"]}
    transition(c, case, "ROLLED_BACK", "verifier", case["body"]["rollback"])


def rollback(cid: str, approver: str) -> str:
    """운영자 비상 롤백 (루프 밖 운영 조치). 루프의 롤백은 auto_rollback이 한다."""
    c = db()
    case = _case(c, cid)
    prev = case["body"]["release"]["prev"]
    base = case["body"]["release"]["version"].split("@")[0]
    registry.set_active(f"{base}@{prev}", f"{cid} 롤백", approver)
    transition(c, case, "ROLLED_BACK", approver, {"to": prev})
    return "ROLLED_BACK"


def list_cases():
    return [dict(r) | {"body": None} for r in db().execute("select * from improvement_case order by updated_at")]


def show(cid):
    c = db()
    case = _case(c, cid)
    ev = [dict(r) for r in c.execute("select from_state,to_state,actor,payload,at from case_event where case_id=? order by at", (cid,))]
    return case, ev
