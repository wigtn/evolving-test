"""SEL 서비스: 개선 케이스 상태머신과 Creator·Improver·Tester·Evaluator·Gate·Promoter·Verifier (앱 설계 3·4·6장).

케이스 = 자산 하나 · 패턴 하나 · 가설 하나. 자산당 활성 케이스는 하나. 상태 전이는 case_event에 추가 기록한다.
저장소는 로컬 SQLite (운영에서는 PostgreSQL sel 스키마, Database Connector 경유).
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
TERMINAL = {"VERIFIED", "REJECTED", "DISMISSED", "ROLLED_BACK"}
CHANGE_LIMITS = {"instruction": 30}


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


def ingest(findings: list[dict]) -> list[str]:
    """중복 키로 묶고, 활성 케이스가 있으면 증거로 붙이고, 없으면 케이스를 연다."""
    c = db()
    opened = []
    for f in findings:
        target = f["target"]["asset_ref"] or f"new/{f['target'].get('procedure')}"
        base = target.split("@")[0]
        live = c.execute("select case_id from improvement_case where (dedupe_key=? or target_asset=?) and state not in (%s)"
                         % ",".join("?" * len(TERMINAL)), (f["dedupe_key"], base, *TERMINAL)).fetchone()
        if live:
            c.execute("insert or ignore into finding values(?,?,?,?,?)", (f["finding_id"], f["dedupe_key"], live[0], json.dumps(f, ensure_ascii=False), _now()))
            continue
        cid = f"case_{uuid.uuid4().hex[:8]}"
        body = {"finding": f, "history": []}
        c.execute("insert into improvement_case values(?,?,?,?,?,?,?,?,?,?)",
                  (cid, base, target.split("@")[1] if "@" in target else None, f["dedupe_key"], "OPEN", 0,
                   f["fidelity"], f["signal"], json.dumps(body, ensure_ascii=False), _now()))
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
    from . import tracing
    ids = {e["run_id"] for e in finding.get("evidence", [])}
    runs = [r for r in tracing.read("runs_prod") + tracing.read("runs_eval") if r["run_id"] in ids][:limit]
    out = []
    for r in runs:
        out.append({"user_message": r["user_message"], "status": r["status"], "final_answer": r["final_answer"][:400],
                    "tool_calls": [{"name": c["name"], "args": c["args"],
                                    "result": (c.get("result") or {}).get("error") if isinstance(c.get("result"), dict) else None}
                                   for c in r["tool_calls"]][:12],
                    "case_kind": (r.get("case_id") or "").split("/")[0] or None})
    return out


def _assets_view():
    b = registry.resolve_agent()
    return {a.ref: a.text for a in [b["agent"], *b["prompts"], *b["skills"], *b["tools"]]}


KINDS = {
    "open_items": ("skill/fi-open-items", "기간을 지정한 미결 조회"),
    "open_items_no_period": ("skill/fi-open-items", "기간 없이 미결 조회 (당월 기본값)"),
    "open_items_annual": ("skill/fi-open-items", "연간·누적·전체 기간 미결 조회"),
    "open_items_ambiguous": ("skill/fi-open-items", "이름이 겹치는 거래처 (되묻기)"),
    "open_items_multi_currency": ("skill/fi-open-items", "통화가 섞인 거래처"),
    "gl_balance": ("skill/fi-gl-balance", "계정 잔액 (월 표현 변환)"),
    "document_post": ("skill/fi-document-post", "전표 전기 (dry_run·승인)"),
    "multi_vendor": ("agent/fi-assistant", "여러 거래처 한 번에 (다단계)"),
}


# ---------- Creator ----------

CREATOR_SYS = """너는 SEL Creator다. 실패 패턴(finding)과 증거 실행 기록, 현행 자산을 보고 가설을 세운다.
고칠 수 있는 범위: Agent는 sel_editable_fields만(composition), SKILL 본문(instruction, example), Prompt는 persona만(instruction),
Tool은 description·인자 설명·pattern(metadata). judge 프롬프트와 평가 케이스는 고칠 수 없다.
원인이 도구 구현·플랫폼 장애·데이터·권한이면 dismiss한다. 증거가 부족해도 dismiss한다.
JSON 하나로만 답한다:
{"decision":"proceed|dismiss","reason":"...","target_asset_ref":"종류/이름@버전","change_type":"instruction|metadata|composition|example|new_asset",
 "field_path":"...","hypothesis":"한 문장","confidence":0.0~1.0,"target_kinds":["..."],"guard_kinds":["..."]}"""


def creator(case) -> dict:
    f = case["body"]["finding"]
    ev = _evidence(f)
    prev = case["body"]["history"][-1] if case["body"]["history"] else None
    if offline():
        return _creator_offline(f, ev, prev)
    user = json.dumps({"finding": {k: f[k] for k in ("signal", "target", "hypothesis_class", "metrics")}, "evidence": ev,
                       "assets": _assets_view(), "case_kinds": {k: v[1] for k, v in KINDS.items()},
                       "previous_iteration": prev}, ensure_ascii=False)
    return _ask("creator", CREATOR_SYS, user)


def infer_kind(msg: str) -> str:
    """오프라인 대역용: 요청 문장만 보고 업무 유형을 추정한다 (정답 라벨을 보지 않는다)."""
    if "전표 올려" in msg:
        return "document_post"
    if "잔액" in msg:
        return "gl_balance"
    if len(re.findall(r"가상거래처-\d{4}", msg)) >= 2:
        return "multi_vendor"
    if any(w in msg for w in ("누적", "연간", "전체")):
        return "open_items_annual"
    if not re.search(r"\d{1,2}월", msg):
        return "open_items_no_period"
    return "open_items"


def _creator_offline(f, ev, prev):
    """LLM 없이 배관을 검증하는 규칙 대역. 증거 실행의 요청 문장과 사용자 발화로 대상을 고른다 (정답지는 보지 않는다)."""
    sig = f["signal"]
    kinds = sorted({e["case_kind"] or infer_kind(e["user_message"]) for e in ev})
    tgt = f["target"]["asset_ref"]
    if sig == "no_asset_match":
        return {"decision": "dismiss", "reason": "offline 모드는 new_asset을 만들지 않는다"}
    if sig == "arg_invalid":
        return {"decision": "proceed", "target_asset_ref": tgt, "change_type": "metadata", "field_path": "description",
                "hypothesis": "도구 설명에 인자 형식이 없어 잘못된 형식으로 호출한다", "confidence": 0.6,
                "target_kinds": kinds or ["gl_balance"], "guard_kinds": []}
    if sig == "tool_error":
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
    if sig == "budget_exceeded":
        return {"decision": "proceed", "target_asset_ref": tgt, "change_type": "composition", "field_path": "max_turns",
                "hypothesis": "턴 상한이 다단계 업무에 부족하다", "confidence": 0.6, "target_kinds": kinds or ["multi_vendor"], "guard_kinds": []}
    # negative_feedback: 세션 이벤트로 대상 결정
    evs = [x for e in f.get("evidence", []) for x in e.get("events", [])]
    fmt = sum(1 for x in evs if x["type"] == "correction")
    ev_kinds = kinds
    if sum("지점" in x.get("utterance", "") for x in evs) * 2 >= len(evs):
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
    kind = base.split("/")[0]
    if h.get("change_type") not in ALLOWED_CHANGE.get(kind, set()):
        return {"decision": "dismiss", "reason": f"{kind}에 허용되지 않는 변경 유형: {h.get('change_type')}"}
    h["target_kinds"] = [k for k in h.get("target_kinds") or [] if k in KINDS]
    h["guard_kinds"] = [k for k in h.get("guard_kinds") or [] if k in KINDS and k not in h["target_kinds"]]
    if not h["target_kinds"]:
        return {"decision": "dismiss", "reason": "목표 업무 유형이 비었거나 알 수 없음"}
    return h


def build_eval_set(h: dict, n_target=12, n_guard=4) -> dict:
    """판정은 holdout 사례로만 한다 (사람이 관리하는 세트, Creator는 내용을 보지 않는다). 실행 전에 고정한다."""
    cs = tester.cases().values()
    hold = [c for c in cs if c["pool"] == "holdout"]
    tk = [c for c in hold if c["kind"] in h.get("target_kinds", [])]
    target = [c["eval_case_id"] for c in sorted(tk, key=lambda c: not _informative(c))][:n_target]
    guard = [c["eval_case_id"] for c in hold if c["kind"] in h.get("guard_kinds", []) and not c["fixed"]][:n_guard]
    fixed = [c["eval_case_id"] for c in hold if c["fixed"]][:4]
    return {"target": target, "guard": guard, "fixed": fixed}


# ---------- Improver ----------

IMPROVER_SYS = """너는 SEL Improver다. 가설과 현행 자산 원문을 받아, 변경 유형 제약 안에서 고친 자산 원문 전체를 돌려준다.
- instruction: 본문만, 변경 30줄 이내, 섹션 단위. front matter의 allowed_tools는 바꾸지 않는다
- metadata: tool의 description, 인자 description, pattern만
- composition: agent의 sel_editable_fields만
- example: 예시 블록만. 예시 값은 '가상' 합성 값만
asset_ref 줄과 front matter 구조는 그대로 둔다 (버전은 시스템이 붙인다).
답 형식: 먼저 한 줄로 변경 요지를 쓰고, 고친 원문 전체를 아래 구분자 사이에 그대로 넣는다. 코드 펜스를 쓰지 않는다.
<<<ASSET
(고친 자산 원문 전체)
ASSET>>>"""


def improver(case, h) -> dict:
    base_ref = h["target_asset_ref"]
    src = registry.load(base_ref)
    if offline():
        return _improver_oracle(src)
    from langchain_core.messages import HumanMessage, SystemMessage
    hyp = {k: h.get(k) for k in ("target_asset_ref", "change_type", "field_path", "hypothesis")}   # 판정 세트는 넘기지 않는다
    msg = json.dumps({"hypothesis": hyp, "asset_text": src.text}, ensure_ascii=False)
    for _ in range(2):
        txt = _text(chat_model("improver").invoke([SystemMessage(IMPROVER_SYS), HumanMessage(msg)]))
        m = re.search(r"<<<ASSET\n(.*?)\nASSET>>>", txt, re.S)
        if m:
            body = re.sub(r"^```\w*\n|\n```$", "", m.group(1).strip("\n"))
            return {"text": body + ("\n" if not body.endswith("\n") else ""), "note": txt[:m.start()].strip()[:300]}
        msg += "\n\n직전 답에 <<<ASSET ... ASSET>>> 블록이 없었다. 형식을 지켜라."
    raise ValueError("Improver 출력에 자산 블록이 없음")


def _improver_oracle(src) -> dict:
    """ORACLE: 같은 자산의 정상 버전 원문으로 되돌린다. 배관 검증 전용이며 개선 능력을 재지 않는다."""
    p = next((DATA / "assets" / registry.FOLDERS[src.kind]).glob(f"{src.name}@*"))
    return {"text": p.read_text(encoding="utf-8"), "note": "ORACLE(offline): 정상 버전 원문 복원"}


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
        registry.parse(new_text if "asset_ref" in new_text else "", src.path)
    except Exception as e:
        return [f"자산 파싱 실패: {repr(e)[:120]}"]
    if change_type not in ALLOWED_CHANGE.get(src.kind, set()):
        errs.append(f"{src.kind}에 허용되지 않는 변경 유형: {change_type}")
    if src.kind == "tool":
        old, new = _tool_meta_paths(yaml.safe_load(src.text)), _tool_meta_paths(yaml.safe_load(new_text))
        bad = [p for p in set(old) | set(new) if old.get(p) != new.get(p) and p != "asset_ref"
               and not (p == "description" or p.endswith(".description") or p.endswith(".pattern"))]
        if bad:
            errs.append(f"tool metadata 밖 변경: {sorted(bad)[:5]}")
    if src.kind == "prompt" and src.meta.get("role") == "judge":
        errs.append("judge 프롬프트는 수정 불가")
    if src.kind == "tool" and change_type != "metadata":
        errs.append("tool은 metadata 변경만 허용")
    a = re.sub(r"asset_ref:\s*\S+", "", src.text).splitlines()
    b = re.sub(r"asset_ref:\s*\S+", "", new_text).splitlines()
    changed = [l for l in difflib.unified_diff(a, b, lineterm="", n=0) if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    if not changed:
        errs.append("변경 없음")
    if change_type == "instruction" and len(changed) > CHANGE_LIMITS["instruction"] * 2:
        errs.append(f"instruction 변경 한도 초과 ({len(changed)}줄)")
    if src.kind == "agent":
        old, new = yaml.safe_load(src.text), yaml.safe_load(new_text)
        allowed = set(old.get("sel_editable_fields", []))
        bad = [k for k in set(old) | set(new) if k not in allowed | {"asset_ref"} and old.get(k) != new.get(k)]
        if bad:
            errs.append(f"agent 허용 필드 밖 변경: {bad}")
    if src.path.suffix == ".md":
        fm = lambda t: {k: v for k, v in yaml.safe_load(re.match(r"^---\n(.*?)\n---", t, re.S).group(1)).items() if k != "asset_ref"}
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
    ids = {e["run_id"] for e in finding.get("evidence", [])}
    tgt = (finding["target"].get("asset_ref") or "").split("@")[0]
    if not tgt:
        return []
    watch = {tgt}
    b = registry.resolve_agent()
    if tgt.startswith("tool/"):                    # 도구 신호의 원인은 SKILL·persona에 있을 수 있다
        watch |= {a.base for a in b["skills"] + b["prompts"]}
    elif tgt.startswith("agent/"):                 # 에이전트 단위 신호(재요청·예산)는 묶음의 어느 자산에서든 올 수 있다
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
        if stale:   # 증거가 이미 교체된 버전에서 나왔다 → 다음 분석에서 새 버전으로 재확인 (앱 설계 STALE)
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
            h["eval_set"] = build_eval_set(h)
            case["body"]["hypothesis"] = h
            transition(c, case, "TRIAGED", "sel_creator", {"hypothesis": h["hypothesis"], "target": h["target_asset_ref"]})
    elif st == "TRIAGED":
        h = case["body"]["hypothesis"]
        src = registry.load(h["target_asset_ref"])
        try:
            out = improver(case, h)
            errs = static_check(src, out["text"], h["change_type"])
        except Exception as e:
            out, errs = None, [f"Improver 실패: {repr(e)[:200]}"]
        if errs:
            case["body"]["last_failure"] = {"iteration": case["iteration"], "static_check": errs}
            transition(c, case, "GATE_FAILED", "sel_improver", {"static_check": errs})
        else:
            ver = _bump(src.version, f"c{cid[-4:]}{case['iteration']}")
            cand = registry.write_candidate(src.ref, ver, out["text"])
            case["body"]["candidate"] = {"ref": cand.ref, "note": out["note"]}
            transition(c, case, "CANDIDATE_READY", "sel_improver", {"candidate": cand.ref})
    elif st == "CANDIDATE_READY":
        transition(c, case, "EVALUATING", "tester")
        h, cand = case["body"]["hypothesis"], case["body"]["candidate"]["ref"]
        base, ver = cand.split("@")
        results = tester.paired_run(h["eval_set"], {base: ver}, k=k, log=log)
        dec = gate.decide(results, case["fidelity"])
        case["body"]["gate"] = dec
        case["body"]["results"] = results
        nxt = {"PASS": "GATE_PASSED", "FAIL": "GATE_FAILED", "HELD": "HELD"}[dec["verdict"]]
        transition(c, case, nxt, "gate", {k2: dec[k2] for k2 in ("G1", "G2", "G3", "G4")} | {"target": dec["target"]})
    elif st == "GATE_PASSED":
        registry.set_lifecycle(case["body"]["candidate"]["ref"], "candidate")
        transition(c, case, "AWAITING_APPROVAL", "sel_service")
    elif st == "GATE_FAILED":
        g = case["body"].pop("gate", None)
        case["body"]["history"].append({"iteration": case["iteration"],
                                        "hypothesis": {k: v for k, v in (case["body"].get("hypothesis") or {}).items() if k != "eval_set"},
                                        "static_check": (case["body"].pop("last_failure", None) or {}).get("static_check"),
                                        "gate": {k: g[k] for k in ("verdict", "G1", "G2", "G3", "G4", "target", "guard")} if g else None})
        case["body"].pop("candidate", None)
        case["body"].pop("results", None)
        transition(c, case, "REJECTED" if case["iteration"] >= gate.MAX_ITER else "OPEN", "sel_orchestrator")
    return _case(c, cid)["state"]


def run_until_wait(cid: str, k: int = 3, log=print) -> str:
    for _ in range(20):
        st = _case(db(), cid)["state"]
        if st in TERMINAL | {"AWAITING_APPROVAL", "HELD", "VERIFYING"}:
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
    transition(c, case, "REJECTED", approver, {"reason": reason})
    return "REJECTED"


def verify(cid: str, after: dict) -> str:
    """after: analyzer 결과(반영 후 배치). 같은 신호의 발생률이 기준 이하로 내려갔는지 본다."""
    c = db()
    case = _case(c, cid)
    f = case["body"]["finding"]
    before = f["metrics"]["rate"]
    same = [x for x in after["findings"] + after["below_threshold"]
            if x["signal"] == f["signal"] and x["dedupe_key"] == f["dedupe_key"]]
    after_rate = max((x["metrics"]["rate"] for x in same), default=0.0)
    ok = after_rate <= max(before / 2, 0.02)
    case["body"]["verification"] = {"rate_before": before, "rate_after": after_rate}
    transition(c, case, "VERIFIED" if ok else "REGRESSED", "verifier", case["body"]["verification"])
    return case["state"]


def rollback(cid: str, approver: str) -> str:
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
