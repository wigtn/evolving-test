"""거버넌스 검사기 (docs/10 W2-3 ~ W2-11).

check(asset, bundle, ...) → Decision
  1) scope 사전 검사: 변경 종류 ↔ 자산 종류 ↔ 가설, 편집 개수
  2) 편집 적용(W1 apply_bundle, 허용 주소는 정책 파일). 주소 · 필드 · 섹션 탈출 위반은 scope로 바꾼다.
     형식 오류(E_SCHEMA 등)는 거버넌스가 아니라 생성 실패라서 EditError를 그대로 올린다.
  3) 내용 검사: size · inflow · safety · leak · grade. 바뀐 부분(추가된 줄 · 문장)만 본다.

규칙 ID는 시험(W3)이 기대값으로 쓴다. 바꾸면 시험도 바꾼다.
  scope : SCOPE-KIND · SCOPE-ADDR · SCOPE-FIELD · SCOPE-ESCAPE
  size  : SIZE-EDITS · SIZE-SECTION · SIZE-DESC · SIZE-ARGDESC
  inflow: INFLOW-HIDDEN · INFLOW-URL · INFLOW-DIRECTIVE · INFLOW-EVIDENCE · INFLOW-TOOL · INFLOW-WRITE-TOOL
  safety: SAFETY-DELETE · SAFETY-WEAKEN
  leak  : LEAK-COPY · LEAK-ID
  grade : GRADE

Improver 재시도에는 feedback()의 범주 이름만 준다(W2-11). 상세(규칙 ID · 매칭 구간 해시)는 판정 로그로만.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..config import DATA, RUNS
from ..edit.address import parse_address
from ..edit.apply import EditError, EditResult, apply_bundle, validate_bundle
from ..edit.sections import block
from . import policy as P
from .normalize import compact, has_invisible, norm, surface

LOG = RUNS / "governance" / "decisions.jsonl"
SCOPE_CODES = {"E_NOT_ALLOWED": "SCOPE-ADDR", "E_NEW_KEY": "SCOPE-FIELD", "E_TYPE_MISMATCH": "SCOPE-FIELD",
               "E_HEADING_IN_TEXT": "SCOPE-ESCAPE", "E_STRUCTURE_CHANGED": "SCOPE-ESCAPE"}
TLDS = ["com", "net", "org", "io", "kr", "co", "ai", "dev", "xyz", "app", "me", "ly", "gg", "info", "biz", "site",
        "online", "link", "page", "ru", "jp", "cn"]
FILE_EXT = {"csv", "xlsx", "xls", "json", "yaml", "yml", "md", "txt", "pdf", "py", "sql", "xml", "html", "log", "zip"}
DOT = r"[.。｡．]"
_TLD_ALT = "|".join(sorted({re.escape(x) for t in TLDS for x in (t, norm(t))}, key=len, reverse=True))
URL_RES = [
    re.compile(r"(?<![a-z0-9])(?!asset\s*:)[a-z][a-z0-9+.-]*\s*[:：]\s*/\s*/", re.I),          # 모든 scheme (asset:// 제외)
    re.compile(r"h\s*t\s*t\s*p\s*s?\s*[:：]\s*/\s*/|w\s*w\s*w\s*" + DOT + r"\s*\w", re.I),
    re.compile(r"[\w.+-]+\s*@\s*[\w-]+\s*" + DOT + r"\s*[a-z]{2,}", re.I),                        # 이메일
    re.compile(r"(?<![a-z0-9-])[a-z0-9-]+(?:\s*" + DOT + r"\s*[a-z0-9-]+)*\s*" + DOT + r"\s*(?:" + _TLD_ALT + r")(?![a-z0-9-])", re.I),
    re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])"),                                     # IPv4
    re.compile(r"data\s*:\s*[a-z]+/[a-z0-9.+-]+", re.I),                                                # data URI
]
GENERIC_DOMAIN = re.compile(r"(?<![a-z0-9._-])[a-z0-9-]+(?:" + DOT + r"[a-z0-9-]+)*" + DOT + r"([a-z]{2,24})(?![a-z0-9-])", re.I)


def _url(s: str) -> str | None:
    """URL · 이메일 · 도메인 · IP. 한글 조사가 바로 붙어도(report-upload.io에) 잡도록 \\b 대신 영숫자 경계를 쓴다."""
    for r in URL_RES:
        m = r.search(s)
        if m:
            return m.group(0)
    for m in GENERIC_DOMAIN.finditer(s):
        if m.group(1).lower() not in FILE_EXT and not m.group(0)[0].isdigit():
            return m.group(0)
    return None
SENT = re.compile(r"(?<=[.!?。])\s+")
LIST_MARK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


@dataclass
class Hit:
    rule: str
    check: str
    detail: str
    span_hash: str | None = None


@dataclass
class Decision:
    ok: bool
    hits: list[Hit]
    policy_ref: str
    policy_hash: str
    base_ref: str
    text: str | None = None
    edit: EditResult | None = None
    checks: dict = field(default_factory=dict)

    @property
    def rules(self) -> set[str]:
        return {h.rule for h in self.hits}


def _h(s: str) -> str:
    return hashlib.sha256(norm(s).encode()).hexdigest()[:16]


def feedback(d: Decision) -> list[str]:
    """Improver 재시도에 돌려주는 것: 검사 항목 이름만."""
    got = {h.check for h in d.hits}
    return [c for c in P.CHECKS if c in got]


def log_decision(d: Decision, candidate_id: str, path: Path | None = None) -> None:
    path = Path(path or LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "candidate_id": candidate_id,
           "base_ref": d.base_ref, "policy_ref": d.policy_ref, "policy_hash": d.policy_hash, "ok": d.ok,
           "checks": d.checks, "hits": [{"rule": h.rule, "check": h.check, "span_hash": h.span_hash} for h in d.hits]}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------- 도구 사전 · 범위 ----------

def _healthy(kind_folder: str):
    from .. import registry
    for p in sorted((DATA / "assets" / kind_folder).glob("*/*@*")):
        yield registry.parse(p.read_text(encoding="utf-8"), p)


def tool_catalog() -> list[dict]:
    out = []
    for t in _healthy("tools"):
        names = {compact(t.name), compact(t.spec.get("name", ""))} - {""}
        out.append({"base": t.base, "names": sorted(names), "write": t.spec.get("side_effect") == "write"})
    return out


def tool_scope(asset) -> set[str]:
    """스킬: front matter allowed_tools. prompt: 그 prompt를 참조하는 모든 매니페스트 spec.tools의 교집합. tool: 자기 자신."""
    from ..registry import base_of
    if asset.kind == "skill":
        return {base_of(r) for r in asset.meta.get("allowed_tools") or []}
    if asset.kind == "tool":
        return {asset.base}
    if asset.kind == "prompt":
        sets = [{base_of(r) for r in a.spec.get("tools", [])} for a in _healthy("agents")
                if a.spec.get("prompt") and base_of(a.spec["prompt"]) == asset.base]
        return set.intersection(*sets) if sets else set()
    return set()


# ---------- 바뀐 부분 ----------

def _units(text: str) -> list[str]:
    out = []
    for line in surface(text).split("\n"):              # 전각 마침표 · 보이지 않는 문자로 문장 나누기를 피하지 못하게
        line = LIST_MARK.sub("", line).strip()
        out += [s.strip() for s in SENT.split(line) if s.strip()]
    return out


def _regions(asset, new_text: str, bundle: dict) -> tuple[str, str]:
    """비교 영역(기존, 새). Markdown은 본문 전체, YAML은 바뀐 리프 값."""
    from .. import registry
    if asset.path.suffix == ".md":
        return asset.body, registry.parse(new_text, asset.path).body
    new = yaml.safe_load(new_text)
    olds, news = [], []
    for e in bundle["edits"]:
        f = parse_address(e["path"]).field
        o, n = asset.meta, new
        for k in f:
            o, n = o[k], n[k]
        olds.append(str(o))
        news.append(str(n))
    return "\n".join(olds), "\n".join(news)


def _added(old: str, new: str, is_md: bool) -> tuple[str, list[str]]:
    """추가된 원문 줄(보이지 않는 문자 검사용)과 추가된 문장(compact 키 기준)."""
    a = old.split("\n") if is_md else _units(old)
    b = new.split("\n") if is_md else _units(new)
    added_raw = []
    for tag, _, _, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            added_raw += b[j1:j2]
    ok_ = {compact(u) for u in _units(old)}
    return "\n".join(added_raw), [u for u in _units(new) if compact(u) not in ok_]


# ---------- 검사 항목 ----------

def _size(pol, asset, bundle, new_text: str) -> list[Hit]:
    from .. import registry
    lim = pol["limits"]
    hits = []
    if asset.path.suffix == ".md":
        a = asset.body.split("\n")
        b = registry.parse(new_text, asset.path).body.split("\n")
        for path in {parse_address(e["path"]).section for e in bundle["edits"]}:
            s1, e1 = block(a, path)
            s2, e2 = block(b, path)
            n = sum(1 for l in difflib.unified_diff(a[s1:e1], b[s2:e2], lineterm="", n=0)
                    if l[:1] in "+-" and not l.startswith(("+++", "---")))
            if n > lim["section_changed_lines"]:
                hits.append(Hit("SIZE-SECTION", "size", f"{'/'.join(path) or '(본문)'}: {n}줄"))
    else:
        for e in bundle["edits"]:
            f = parse_address(e["path"]).field
            v = e["value"]
            if not isinstance(v, str):
                continue
            if f == ("spec", "description") and len(v) > lim["description_chars"]:
                hits.append(Hit("SIZE-DESC", "size", f"{len(v)}자"))
            if f[-1] == "description" and f[:3] == ("spec", "input_schema", "properties") \
                    and len(v) > lim["arg_description_chars"]:
                hits.append(Hit("SIZE-ARGDESC", "size", f"{f[3]}: {len(v)}자"))
    return hits


def _grams(s: str, n: int) -> set[str]:
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _inflow(pol, added_raw: str, fenced_removed, catalog, scope) -> list[Hit]:
    hits = []
    if not added_raw.strip():
        return hits
    inf = pol["inflow"]
    surf, nrm, cmp_ = surface(added_raw), norm(added_raw), compact(added_raw)
    if has_invisible(added_raw) or re.search(r"<!--|<\s*[a-zA-Z/!?]", surf) or re.search(r"<!--|<\s*[a-z/!?]", nrm):
        hits.append(Hit("INFLOW-HIDDEN", "inflow", "보이지 않는 문자 · HTML 주석 · 태그", _h(added_raw)))
    dotted = re.sub(DOT, ".", added_raw)                   # 。 ｡ ． → . (skeleton이 다른 글자로 바꾸기 전에)
    for s in (surf, nrm, surface(dotted), norm(dotted), re.sub(r"\s*\n\s*", "", surf)):   # 줄 나눔 우회도 본다
        m = _url(s)
        if m:
            hits.append(Hit("INFLOW-URL", "inflow", "URL · 이메일 · 도메인 · IP", _h(m)))
            break
    for p in inf["directive_patterns"]:
        if compact(p) and compact(p) in cmp_:
            hits.append(Hit("INFLOW-DIRECTIVE", "inflow", "지시문 의심", _h(p)))
            break
    n, th = inf["evidence_ngram"], inf["evidence_containment"]
    ga = _grams(cmp_, n)
    for s in fenced_removed:
        gs = _grams(compact(s), n)
        if gs and len(gs & ga) / len(gs) >= th:
            hits.append(Hit("INFLOW-EVIDENCE", "inflow", "구획에서 지운 문장과 유사", _h(s)))
            break
    for t in catalog:
        if t["base"] in scope:
            continue
        if any(nm in cmp_ for nm in t["names"]):
            rule = "INFLOW-WRITE-TOOL" if t["write"] else "INFLOW-TOOL"
            hits.append(Hit(rule, "inflow", f"범위 밖 도구: {t['base']}", _h(t["base"])))
    return hits


def _count(s: str, sub: str) -> int:
    return s.count(sub) if sub else 0


NEUTRALIZE = [
    (re.compile(r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1[^\n]*$", re.M | re.S), ""),   # 닫힌 코드 펜스
    (re.compile(r"^ {0,3}(`{3,}|~{3,}).*\Z", re.M | re.S), ""),                        # 안 닫힌 코드 펜스
    (re.compile(r"~~.*?~~", re.S), ""),                                                # 취소선
    (re.compile(r"<!--.*?(?:-->|\Z)", re.S), ""),                                       # HTML 주석
    (re.compile(r"^ {0,3}>.*$", re.M), ""),                                            # 인용
]


def _effective(text: str) -> str:
    """안전 문장으로 '살아 있는' 글. 펜스 · 취소선 · 주석 · 인용 안으로 옮긴 안전 문장은 지운 것으로 본다."""
    t = surface(text)
    for r, rep in NEUTRALIZE:
        t = r.sub(rep, t)
    return t


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()


def _safety(pol, old: str, new: str, added_raw: str, added_units) -> list[Hit]:
    sp = pol["safety"]
    kws = [k for k in {compact(k) for k in sp["keywords"]} if k]
    weak = [w for w in {compact(w) for w in sp["weakening_phrases"]} if w]
    cond = [w for w in {compact(w) for w in sp["conditional_phrases"]} if w]
    strong = [x for x in {compact(x) for x in sp["strong_markers"]} if x]
    oe, ne = _effective(old), _effective(new)
    co, cn = compact(oe), compact(ne)
    n_strong = lambda c: sum(_count(c, x) for x in strong)  # noqa: E731
    hits = []

    def add(rule, detail, key=None):
        if not any(h.rule == rule for h in hits):
            hits.append(Hit(rule, "safety", detail, _h(key) if key else None))

    # 1) 키워드 개수가 줄었다
    dropped = [k for k in kws if _count(cn, k) < _count(co, k)]
    if dropped:
        add("SAFETY-DELETE", "안전 키워드 개수 감소", ",".join(sorted(dropped)))
    # 2) 기존 안전 문장마다 새 본문에서 짝을 찾는다(개수를 다른 곳에서 채우는 우회를 막는다)
    new_units = [compact(u) for u in _units(ne)]
    for r in _units(oe):
        cr = compact(r)
        if not any(k in cr for k in kws) or cr in new_units:
            continue
        inside = [u for u in new_units if cr in u]          # 문장 앞뒤에 조건을 덧붙인 경우
        best = inside[0] if inside else max(new_units, key=lambda u: _ratio(cr, u), default="")
        if not inside and _ratio(cr, best) < 0.6:
            add("SAFETY-DELETE", "안전 문장이 사라짐", r)
        elif n_strong(best) < n_strong(cr):
            add("SAFETY-WEAKEN", "안전 문장의 부정 · 강제 표지 삭제", r)
        elif any(_count(best, w) > _count(cr, w) for w in weak + cond):
            add("SAFETY-WEAKEN", "안전 문장에 조건 · 완화 표현 추가", r)
    # 3) 새로 들어온 완화 표현이 안전 키워드 근처에 있다(다른 곳에서 덮어쓰기). 펜스 안도 모델은 읽으므로 원문 추가분 전체를 본다
    ca = compact(added_raw) + "\u0000" + "".join(compact(u) for u in added_units)
    W = sp["window"]
    new_weak = [w for w in weak if _count(compact(new), w) > _count(compact(old), w)]
    for k in kws:
        for m in re.finditer(re.escape(k), ca):
            w = next((w for w in new_weak if w in ca[max(0, m.start() - W):m.end() + W]), None)
            if w:
                add("SAFETY-WEAKEN", "안전 문장 근처에 완화 표현 추가", k + "|" + w)
                break
    # 4) 안전 문장 전체의 부정 · 강제 표지 개수가 줄었다(문장을 합쳐 짝짓기를 피하는 우회)
    def strong_in_safety(text):
        return sum(n_strong(cu) for cu in (compact(u) for u in _units(text)) if any(k in cu for k in kws))
    if strong_in_safety(ne) < strong_in_safety(oe):
        add("SAFETY-WEAKEN", "안전 문장의 부정 · 강제 표지 개수 감소")
    return hits


def _leak(pol, asset, added_raw: str, evidence) -> list[Hit]:
    hits = []
    if not added_raw.strip():
        return hits
    n = pol["leak"]["copy_ngram"]
    ca, co = compact(added_raw), compact(asset.text)
    evs = [compact(e.get("text") or "") for e in evidence]
    for i in range(len(ca) - n + 1):
        g = ca[i:i + n]
        if g not in co and any(g in e for e in evs):
            hits.append(Hit("LEAK-COPY", "leak", f"증거와 {n}자 이상 겹침", _h(g)))
            break
    reserved = [re.compile(r["pattern"]) for r in pol["synthetic"]["reserved_numbers"]]
    surf = surface(added_raw)
    for s in (surf, surf.replace("\n", "")):          # 줄을 나눠 번호를 쪼갠 우회도 본다
        taken: list[tuple[int, int]] = []
        for ident in pol["leak"]["identifiers"]:
            for m in re.finditer(ident["pattern"], s):
                if any(a < m.end() and m.start() < b for a, b in taken):
                    continue
                digits = re.sub(r"\D", "", m.group(0))
                if len(digits) < ident.get("min_digits", 0):
                    continue
                taken.append(m.span())
                if ident["id"] == "number" and _exempt_number(pol, digits, s, m.end(), reserved):
                    continue
                hits.append(Hit("LEAK-ID", "leak", f"식별자 형식: {ident['name']}", _h(m.group(0))))
        if hits and hits[-1].rule == "LEAK-ID":
            break
    return hits


def _exempt_number(pol, digits: str, s: str, end: int, reserved) -> bool:
    """번호 하나(토큰) 단위 면제: 합성 예약 범위, 날짜(YYYYMMDD), 금액 단위가 바로 붙은 숫자."""
    if any(r.fullmatch(digits) for r in reserved):
        return True
    ex = pol["leak"]["exempt"]
    if ex["dates"] and len(digits) == 8:
        y, mo, d = int(digits[:4]), int(digits[4:6]), int(digits[6:])
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return True
    after = s[end:end + 6].lstrip().lower()
    return any(after.startswith(u.lower()) for u in ex["amount_units"])


def _grade(pol, asset, evidence) -> list[Hit]:
    hits = []
    ag = asset.grade
    lowest = pol["grade"]["most_sensitive"] == "lowest"
    for e in evidence:
        g = e.get("grade")
        if not isinstance(g, int) or not isinstance(ag, int):
            hits.append(Hit("GRADE", "grade", "등급 모름 → 가장 민감으로 처리"))
            break
        if (g < ag) if lowest else (g > ag):
            hits.append(Hit("GRADE", "grade", f"증거 등급 {g}이 자산 등급 {ag}보다 민감"))
            break
    return hits


# ---------- 진입점 ----------

def check(asset, bundle: dict, *, change_kind: str | None = None, evidence=(), fenced_removed=(),
          pol: P.Policy | None = None, catalog=None, scope=None) -> Decision:
    """asset: 기준 버전(registry.Asset). change_kind: 가설의 변경 종류(없으면 묶음 값을 믿는다).
    evidence: Improver가 본 증거 [{text, grade}]. fenced_removed: 증거 구획(W4)이 지운 문장 목록."""
    pol = pol or P.default()
    validate_bundle(bundle)                                   # 형식 오류는 생성 실패(EditError)
    d = Decision(False, [], pol.ref, pol.hash, asset.ref)
    ck = bundle["change_kind"]
    if pol["change_kinds"].get(ck) != asset.kind:
        d.hits.append(Hit("SCOPE-KIND", "scope", f"{ck}는 {asset.kind}에 쓸 수 없다"))
    if change_kind and change_kind != ck:
        d.hits.append(Hit("SCOPE-KIND", "scope", f"가설 {change_kind} ≠ 묶음 {ck}"))
    if asset.kind == "skill":
        for e in bundle["edits"]:
            try:
                a = parse_address(e["path"])
            except Exception:
                continue
            if a.mode == "section" and (ck == "example") != P.is_example_section(pol, a.section):
                d.hits.append(Hit("SCOPE-KIND", "scope", f"{ck}와 섹션 {'/'.join(a.section)}이 맞지 않음"))
                break
    if len(bundle["edits"]) > pol["limits"]["edits_per_bundle"]:
        d.hits.append(Hit("SIZE-EDITS", "size", f"편집 {len(bundle['edits'])}개"))
    try:
        res = apply_bundle(asset, bundle, P.allowed_fn(pol))
    except EditError as x:
        if x.code not in SCOPE_CODES:
            raise
        d.hits.append(Hit(SCOPE_CODES[x.code], "scope", x.code))
        return _finish(d)
    d.text, d.edit = res.text, res
    is_md = asset.path.suffix == ".md"
    old, new = _regions(asset, res.text, bundle)
    added_raw, added_u = _added(old, new, is_md)
    catalog = tool_catalog() if catalog is None else catalog
    scope = tool_scope(asset) if scope is None else scope
    d.hits += _size(pol, asset, bundle, res.text)
    d.hits += _inflow(pol, added_raw, fenced_removed, catalog, scope)
    d.hits += _safety(pol, old, new, added_raw, added_u)
    d.hits += _leak(pol, asset, added_raw, evidence)
    d.hits += _grade(pol, asset, evidence)
    return _finish(d, evidence_given=bool(evidence))


def _finish(d: Decision, evidence_given: bool = False) -> Decision:
    """검사 요약. 실행하지 않은 검사를 pass로 적지 않는다: 증거가 없으면 grade는 not_evaluated,
    leak은 식별자만 봤으므로 partial. 적용 전에 막히면 내용 검사는 skip."""
    failed = {h.check for h in d.hits}
    out = {}
    for c in P.CHECKS:
        if c in failed:
            out[c] = "fail"
        elif d.text is None and c not in ("scope", "size"):
            out[c] = "skip"
        elif c == "grade" and not evidence_given:
            out[c] = "not_evaluated"
        elif c == "leak" and not evidence_given:
            out[c] = "partial"
        else:
            out[c] = "pass"
    d.checks = out
    d.ok = not d.hits
    return d


def reject_before_generation(asset, address: str, pol: P.Policy | None = None) -> Decision:
    """가설 대상 주소가 정책 밖이라 생성하지 않은 경우의 판정(판정 로그에 남기려고)."""
    pol = pol or P.default()
    d = Decision(False, [Hit("SCOPE-ADDR", "scope", "가설 대상 주소가 정책 밖", _h(address))], pol.ref, pol.hash, asset.ref)
    return _finish(d)


def target_allowed(asset, address: str | None, pol: P.Policy | None = None) -> bool:
    """가설의 대상 주소가 정책상 바꿀 수 있는 곳인가 (생성 전에 막는다). 주소를 모르면 True(생성 뒤 검사에 맡김)."""
    if not address:
        return True
    try:
        a = parse_address(address)
    except Exception:
        return True
    return P.allowed(asset, a, pol)
