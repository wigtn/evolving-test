"""가짜 데이터 생성기: 합성 SAP 세계, 평가 케이스, 결함 버전 자산, finding fixture.

python gen/build.py            # data/ 아래 생성 + 검증
python gen/build.py --check    # 검증만

모든 값은 고정 seed로 결정적이다. ID는 예약 범위(거래처 9xxxxxxx, 계정 9xxxxxx, 전표 99xxxxxxxx)만 쓴다.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import re
import shutil
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WORLD_ID = "corp-sim@0.1"
TODAY = date(2026, 10, 27)
CUR_PERIOD = TODAY.strftime("%Y-%m")
SEED = 20261027
ROW_LIMIT = 100

CITIES = ["서울", "부산", "대전", "광주", "인천", "울산", "수원", "청주"]
EXPENSE_ACCOUNTS = [
    ("9510000", "가상소모품비"), ("9520000", "가상운반비"), ("9530000", "가상외주가공비"),
    ("9540000", "가상지급수수료"), ("9550000", "가상수선비"),
]
AP_ACCOUNT = ("9210000", "가상외상매입금")


# ---------- 공통 ----------

def write_jsonl(path: Path, rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def fmt_amount(minor: int, waers: str) -> str:
    """persona의 금액 표기 규칙: 원은 전체 숫자 + 쉼표, 외화는 소수 둘째 자리 + 통화 코드."""
    if waers == "KRW":
        return f"{minor:,}원"
    return f"{minor // 100:,}.{minor % 100:02d} {waers}"


def period_of(d: str) -> str:
    return d[:7]


def months_between(start: str, end: str):
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def stable_id(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12]


# ---------- 세계 ----------

def build_world(rng: random.Random):
    company_codes = [
        {"bukrs": "9100", "name": "가상법인-A", "currency": "KRW"},
        {"bukrs": "9200", "name": "가상법인-B", "currency": "KRW", "foreign": ["USD"]},
    ]
    gl_accounts = [{"saknr": s, "text": t, "kind": "expense"} for s, t in EXPENSE_ACCOUNTS]
    gl_accounts.append({"saknr": AP_ACCOUNT[0], "text": AP_ACCOUNT[1], "kind": "payable"})

    vendors = []
    for i in range(1, 121):
        bukrs = "9100" if i <= 80 else "9200"
        vendors.append({
            "lifnr": f"9{i:07d}", "name1": f"가상거래처-{i:04d}",
            "ort01": rng.choice(CITIES), "bukrs": bukrs,
            "profile": "multi_currency" if bukrs == "9200" else "normal",
        })
    # 이름이 겹치는 거래처 그룹 (D02: 거래처 모호)
    for base, branches in [("0418", ["서울", "부산"]), ("0520", ["대전", "광주", "인천"])]:
        for j, city in enumerate(branches):
            vendors.append({
                "lifnr": f"9{int(base) + j:07d}", "name1": f"가상거래처-{base} {city}지점",
                "ort01": city, "bukrs": "9100", "profile": "ambiguous", "group": base,
            })
    # 미결 전표가 많은 거래처 (D01: 기간 없이 조회하면 row limit 초과)
    heavy = {"90000011", "90000023", "90000042"}
    for v in vendors:
        if v["lifnr"] in heavy:
            v["profile"] = "heavy"

    documents = []
    seq = 1
    start = date(2025, 1, 1)
    span_days = (TODAY - start).days
    for v in vendors:
        if v["profile"] == "heavy":
            n = rng.randint(260, 380)
        elif v["profile"] == "ambiguous":
            n = rng.randint(6, 14)
        else:
            n = rng.randint(4, 30)
        for _ in range(n):
            budat = start + timedelta(days=rng.randint(0, span_days))
            # heavy 거래처도 당월분은 100행 미만이 되도록 당월 비중을 낮춘다
            if v["profile"] == "heavy" and period_of(budat.isoformat()) == CUR_PERIOD and rng.random() < 0.7:
                budat -= timedelta(days=40)
            waers = "USD" if v["profile"] == "multi_currency" and rng.random() < 0.45 else "KRW"
            if waers == "KRW":
                minor = rng.randint(10_000, 5_000_000) * 10
            else:
                minor = rng.randint(10_000, 2_000_000)  # cents
            exp_acct = rng.choice(EXPENSE_ACCOUNTS)[0]
            age = (TODAY - budat).days
            status = "open" if rng.random() < (0.85 if age < 60 else 0.35) else "cleared"
            documents.append({
                "belnr": f"99{seq:08d}", "bukrs": v["bukrs"], "gjahr": budat.year,
                "budat": budat.isoformat(), "lifnr": v["lifnr"], "waers": waers,
                "status": status,
                "lines": [
                    {"saknr": exp_acct, "side": "D", "amount_minor": minor},
                    {"saknr": AP_ACCOUNT[0], "side": "C", "amount_minor": minor, "lifnr": v["lifnr"]},
                ],
            })
            seq += 1

    # 계정별 월말 잔액 (누적, 통화별). 비용 계정은 차변 +, 외상매입금은 대변 +
    bal = defaultdict(int)
    for d in documents:
        for ln in d["lines"]:
            sign = 1 if (ln["side"] == "D") == (ln["saknr"] != AP_ACCOUNT[0]) else -1
            bal[(d["bukrs"], ln["saknr"], d["waers"], period_of(d["budat"]))] += sign * ln["amount_minor"]
    gl_balances = []
    keys = {(b, s, w) for (b, s, w, _) in bal}
    for b, s, w in sorted(keys):
        run = 0
        for p in months_between("2025-01", CUR_PERIOD):
            run += bal.get((b, s, w, p), 0)
            gl_balances.append({"bukrs": b, "saknr": s, "waers": w, "period": p, "balance_minor": run})

    return {
        "company_codes": company_codes, "gl_accounts": gl_accounts, "vendors": vendors,
        "documents": documents, "gl_balances": gl_balances,
    }


# ---------- 참조 풀이 (정답 계산) ----------

class World:
    def __init__(self, w):
        self.w = w
        self.vendors = {v["lifnr"]: v for v in w["vendors"]}
        self.docs_by_vendor = defaultdict(list)
        for d in w["documents"]:
            self.docs_by_vendor[d["lifnr"]].append(d)
        self.gl = {(g["bukrs"], g["saknr"], g["waers"], g["period"]): g["balance_minor"] for g in w["gl_balances"]}

    def search_vendor(self, q):
        return [v for v in self.w["vendors"] if q in v["name1"]]

    def open_items(self, lifnr, p_from=None, p_to=None):
        rows = []
        for d in self.docs_by_vendor[lifnr]:
            if d["status"] != "open":
                continue
            p = period_of(d["budat"])
            if p_from and p < p_from:
                continue
            if p_to and p > p_to:
                continue
            rows.append(d)
        return rows

    def totals(self, rows):
        t = defaultdict(lambda: {"count": 0, "amount_minor": 0})
        for d in rows:
            t[d["waers"]]["count"] += 1
            t[d["waers"]]["amount_minor"] += d["lines"][0]["amount_minor"]
        return {k: {**v, "display": fmt_amount(v["amount_minor"], k)} for k, v in sorted(t.items())}


# ---------- 평가 케이스 ----------

def build_cases(world: World, rng: random.Random):
    cases = []
    normal = [v for v in world.w["vendors"] if v["profile"] == "normal"]
    multi = [v for v in world.w["vendors"] if v["profile"] == "multi_currency"]
    heavy = [v for v in world.w["vendors"] if v["profile"] == "heavy"]
    users = ["U9001", "U9002", "U9003", "U9004", "U9005", "U9006", "U9007", "U9008"]

    def add(kind, n_idx, msg, expected, checks, covers, fixed=False, user=None):
        cid = f"{kind}/{n_idx:04d}"
        cases.append({
            "eval_case_id": cid, "agent_asset_id": "asset://agent/fi/assistant", "source": "sme",
            "kind": kind, "world": WORLD_ID,
            "input": {"user_message": msg, "user_id": user or rng.choice(users), "today": TODAY.isoformat()},
            "expected": expected, "checks": checks, "rubric_id": "fi-answer@1",
            "label_status": "labeled", "grade": 0, "covers_defects": covers, "fixed": fixed,
        })

    # 1) 기간 지정 조회
    for i in range(1, 31):
        v = rng.choice(normal)
        p = rng.choice(list(months_between("2026-03", "2026-09")))
        rows = world.open_items(v["lifnr"], p, p)
        add("open_items", i, f"{v['name1']} {int(p[5:])}월 미결 전표 합계 알려줘",
            {"vendor": v["lifnr"], "period": [p, p], "answer": world.totals(rows),
             "tool_calls_must_include": ["asset://tool/fi/open-items-query"]},
            ["grounded", "format", "currency_separated"], ["D03", "D10"])

    # 2) 기간 생략 → 당월 기본값 (D01 목표)
    targets = heavy * 4 + rng.sample(normal, 8)
    for i, v in enumerate(targets, 1):
        rows = world.open_items(v["lifnr"], CUR_PERIOD, CUR_PERIOD)
        add("open_items_no_period", i, f"{v['name1']} 미결 얼마야?",
            {"vendor": v["lifnr"], "period": [CUR_PERIOD, CUR_PERIOD], "answer": world.totals(rows),
             "must_state": "당월", "must_not_error": "ROW_LIMIT_EXCEEDED",
             "tool_args_must_include": {"period_from": CUR_PERIOD}},
            ["grounded", "format", "scope_stated"], ["D01"], user="U9003")

    # 3) 연간·누적 조회 (D01 과잉 수정 guard: "항상 당월"로 고치면 회귀)
    for i in range(1, 16):
        v = heavy[i % 3] if i <= 6 else rng.choice(normal)
        phrase, rng_p = rng.choice([("올해 누적", ("2026-01", CUR_PERIOD)), ("연간", ("2026-01", CUR_PERIOD)), ("전체 기간", (None, None))])
        rows = world.open_items(v["lifnr"], *rng_p)
        add("open_items_annual", i, f"{v['name1']} {phrase} 미결 합계",
            {"vendor": v["lifnr"], "period": list(rng_p), "answer": world.totals(rows),
             "must_not_restrict_to": CUR_PERIOD,
             "note": "결과가 100행을 넘으면 기간을 나눠 재조회해야 한다" if len(rows) > ROW_LIMIT else None},
            ["grounded", "format", "scope_stated"], ["D01"], user="U9004")

    # 4) 거래처 모호 (D02 목표, user simulator 필요)
    groups = defaultdict(list)
    for v in world.w["vendors"]:
        if v["profile"] == "ambiguous":
            groups[v["group"]].append(v)
    i = 0
    for base, members in groups.items():
        for target in members:
            for p in ["2026-08", "2026-09"]:
                i += 1
                rows = world.open_items(target["lifnr"], p, p)
                add("open_items_ambiguous", i, f"가상거래처-{base} {int(p[5:])}월 미결 알려줘",
                    {"must_clarify": True, "candidates": [m["lifnr"] for m in members],
                     "simulator_reply": f"{target['ort01']}지점이요",
                     "vendor": target["lifnr"], "period": [p, p], "answer": world.totals(rows)},
                    ["clarifies_ambiguity", "grounded", "format"], ["D02"], user="U9007")

    # 5) 다통화 (D10 목표)
    for i in range(1, 16):
        v = rng.choice(multi)
        p = rng.choice(["2026-07", "2026-08", "2026-09"])
        rows = world.open_items(v["lifnr"], p, p)
        add("open_items_multi_currency", i, f"{v['name1']} {int(p[5:])}월 미결 정리해줘",
            {"vendor": v["lifnr"], "period": [p, p], "answer": world.totals(rows)},
            ["currency_separated", "grounded", "format"], ["D10", "D03"], user="U9008")

    # 6) 계정 잔액 (D06 목표: period 형식)
    phrasings = [("9월", "2026-09"), ("지난달", "2026-09"), ("2026년 8월", "2026-08"), ("7월말", "2026-07")]
    for i in range(1, 21):
        saknr, text = rng.choice(EXPENSE_ACCOUNTS)
        bukrs = rng.choice(["9100", "9200"])
        phrase, p = rng.choice(phrasings)
        balance = world.gl.get((bukrs, saknr, "KRW", p), 0)
        add("gl_balance", i, f"{bukrs} 회사 {text}({saknr}) {phrase} 잔액",
            {"saknr": saknr, "bukrs": bukrs, "period": p,
             "answer": {"balance_minor": balance, "waers": "KRW", "display": fmt_amount(balance, "KRW")},
             "tool_args_must_match": {"period": r"^\d{4}-\d{2}$"}},
            ["grounded", "format"], ["D06", "D08"], user="U9005")

    # 7) 전표 전기 (D04 목표, 고정 세트: SME 필수 사례)
    for i in range(1, 16):
        v = rng.choice(normal)
        saknr, text = rng.choice(EXPENSE_ACCOUNTS)
        minor = rng.randint(1_000, 90_000) * 100
        add("document_post", i, f"9100 회사 오늘자로 {text} {fmt_amount(minor, 'KRW')} 전표 올려줘. 거래처 {v['name1']} 외상",
            {"lines": [{"saknr": saknr, "side": "D", "amount_minor": minor},
                       {"saknr": AP_ACCOUNT[0], "side": "C", "amount_minor": minor, "lifnr": v["lifnr"]}],
             "call_order": ["dry_run", "approval_request", "post_with_token"],
             "forbidden": ["post_without_approval_token"]},
            ["policy", "grounded"], ["D04"], fixed=True, user="U9006")

    # 8) 다단계 (D05 목표: max_turns 부족)
    for i in range(1, 11):
        vs = rng.sample(normal, 5)
        p = "2026-09"
        per = {v["lifnr"]: world.totals(world.open_items(v["lifnr"], p, p)) for v in vs}
        add("multi_vendor", i, ", ".join(v["name1"] for v in vs) + " 각각 9월 미결 합계 표로",
            {"period": [p, p], "answer_by_vendor": per, "min_tool_calls": 10},
            ["grounded", "format"], ["D05"])

    # pool: 가짜 운영 트래픽이 요청을 뽑는 풀(dev)과 아닌 풀. 판정 분할(held-out)은 sel.split_golden이 노출 원장으로 따로 한다
    for c in cases:
        c["pool"] = "holdout" if int(stable_id(c["eval_case_id"]), 16) % 2 else "dev"
    return cases


def corrupt_labels(cases, catalog):
    """D08: 골든 라벨 하나를 일부러 틀리게. 깨끗한 라벨은 ground_truth에 남긴다."""
    clean = copy.deepcopy(cases)
    for d in catalog["defects"]:
        if d["patch"]["op"] != "corrupt_label":
            continue
        sel = d["patch"]["case_selector"]
        for c in cases:
            if c["eval_case_id"].endswith(sel) or c["eval_case_id"] == sel:
                a = c["expected"]["answer"]
                a["balance_minor"] = int(a["balance_minor"] * 0.92)  # 환산 전 값처럼 보이게 8% 차이
                a["display"] = fmt_amount(a["balance_minor"], a["waers"])
                c["_corrupted_by"] = d["id"]
    return cases, clean


# ---------- 결함 버전 자산 ----------

FOLDERS = {"skill": "skills", "prompt": "prompts", "tool": "tools", "agent": "agents"}
MARKER = re.compile(r"^<!-- rule:[\w_]+ -->\n", re.M)


def split_ref(ref: str):
    """asset://종류/ns/이름@버전 → (종류, ns, 이름, 버전). 기준 문서 형식 (W0)."""
    m = re.fullmatch(r"asset://(\w+)/([\w-]+)/([\w-]+)@(.+)", ref)
    if not m:
        raise ValueError(f"자산 참조 형식 아님: {ref}")
    return m.groups()


def asset_path(ref: str) -> Path:
    """원본(규칙 마커 포함)은 data/assets_src/<종류>s/<ns>/. 루프가 보는 자산은 마커를 지운 data/assets/."""
    kind, ns, name, ver = split_ref(ref)
    for ext in (".md", ".yaml"):
        p = DATA / "assets_src" / FOLDERS[kind] / ns / f"{name}@{ver}{ext}"
        if p.exists():
            return p
    raise FileNotFoundError(ref)


def set_path(d: dict, dotted: str, value):
    """YAML 자산에서 점 경로(spec.params.max_turns)의 값을 바꾼다."""
    keys = dotted.split(".")
    for k in keys[:-1]:
        d = d[k]
    d[keys[-1]] = value


def remove_rule(text: str, rule: str) -> str:
    pat = re.compile(rf"^<!-- rule:{rule} -->\n.*\n", re.M)
    out, n = pat.subn("", text)
    if n != 1:
        raise ValueError(f"rule {rule} not found exactly once")
    return out


def clean(text: str, suffix: str) -> str:
    """루프에 노출되는 자산: 규칙 마커 제거, YAML은 같은 서식으로 다시 쓴다 (결함 버전과 서식 차이가 힌트가 되지 않게)."""
    if suffix == ".md":
        return MARKER.sub("", text)
    return yaml.safe_dump(yaml.safe_load(text), allow_unicode=True, sort_keys=False)


def build_healthy():
    n = 0
    for folder in FOLDERS.values():
        dst = DATA / "assets" / folder
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        for p in (DATA / "assets_src" / folder).glob("*/*@*"):
            out = dst / p.parent.name / p.name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(clean(p.read_text(encoding="utf-8"), p.suffix), encoding="utf-8")
            n += 1
    return n


def build_variants(catalog):
    out_dir = DATA / "assets" / "variants"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for d in catalog["defects"]:
        if not d.get("degraded_version"):
            continue
        src = asset_path(d["target"])
        kind, ns, name, _ = split_ref(d["target"])
        new_ref = f"{d['target'].split('@')[0]}@{d['degraded_version']}"
        text = src.read_text(encoding="utf-8")
        op = d["patch"]["op"]
        if op == "remove_rule":
            text = remove_rule(text, d["patch"]["rule"])
        elif op == "replace_rule":
            pat = re.compile(rf"(^<!-- rule:{d['patch']['rule']} -->\n)(.*\n)+?(?=\Z)", re.M)
            text, n = pat.subn(lambda m: m.group(1) + d["patch"]["value"] + "\n", text)
            if n != 1:
                raise ValueError(d["id"])
        elif op in ("set_field", "replace"):
            y = yaml.safe_load(text)
            set_path(y, d["patch"]["field"], d["patch"]["value"])
            if d["patch"].get("also_remove_pattern"):            # 인자 정의를 형식 없는 문자열로 (D06)
                set_path(y, d["patch"]["also_remove_pattern"], {"type": "string"})
            text = yaml.safe_dump(y, allow_unicode=True, sort_keys=False)
        else:
            raise ValueError(op)
        text = clean(text.replace(d["target"], new_ref), src.suffix)
        dst = out_dir / FOLDERS[kind] / ns / f"{name}@{d['degraded_version']}{src.suffix}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        made.append({"defect": d["id"], "base": d["target"], "degraded": new_ref, "file": str(dst.relative_to(ROOT))})
    return made


# ---------- finding fixture ----------

def build_findings(catalog, rng):
    """분석기 없이 SEL 서비스(케이스 상태머신)를 시험하기 위한 finding. finding 형식."""
    rows = []
    for d in catalog["defects"]:
        e = d["expect"]
        if "signal" not in e:
            continue
        target = f"{d['target'].split('@')[0]}@{d['degraded_version']}" if d.get("degraded_version") else d["target"]
        support = rng.randint(40, 400)
        rate = round(rng.uniform(0.04, 0.12), 3)
        rows.append({
            "finding_id": f"fx-{d['id']}-{stable_id(d['id'])}",
            "analyzer": {"name": "fixture", "version": "0.1.0"},
            "window": {"from": "2026-10-20T00:00:00Z", "to": "2026-10-27T00:00:00Z"},
            "fidelity": e.get("fidelity", "full"), "signal": e["signal"],
            "target": {"asset_ref": None if e.get("change_type") == "new_asset" else target, "channel": "runtime",
                       **({"proposed_kind": "skill"} if e.get("change_type") == "new_asset" else {})},
            "hypothesis_class": e.get("hypothesis_class"),
            "metrics": {"support": support, "distinct_principals": rng.randint(3, 8), "rate": rate,
                        "baseline_rate": round(rate / rng.uniform(2.0, 4.0), 3), "reach_weekly": rng.randint(800, 5000)},
            "evidence": [], "max_grade": 0,
            "platform_signal": bool(e.get("platform_signal")),
            "_ground_truth": {"defect": d["id"], "expected_fix": e.get("fix"), "route": e.get("route", "sel")},
        })
    return rows


# ---------- 검증 ----------

def check(world_raw, cases):
    errs = []
    w = World(world_raw)
    ids = set()
    for d in world_raw["documents"]:
        if not re.fullmatch(r"99\d{8}", d["belnr"]):
            errs.append(f"belnr range {d['belnr']}")
        if d["belnr"] in ids:
            errs.append(f"dup belnr {d['belnr']}")
        ids.add(d["belnr"])
        dr = sum(l["amount_minor"] for l in d["lines"] if l["side"] == "D")
        cr = sum(l["amount_minor"] for l in d["lines"] if l["side"] == "C")
        if dr != cr:
            errs.append(f"unbalanced {d['belnr']}")
    for v in world_raw["vendors"]:
        if not re.fullmatch(r"9\d{7}", v["lifnr"]) or not v["name1"].startswith("가상거래처-"):
            errs.append(f"vendor {v['lifnr']}")
    for h in [v for v in world_raw["vendors"] if v["profile"] == "heavy"]:
        if len(w.open_items(h["lifnr"])) <= ROW_LIMIT:
            errs.append(f"heavy {h['lifnr']} does not exceed row limit")
        if len(w.open_items(h["lifnr"], CUR_PERIOD, CUR_PERIOD)) > ROW_LIMIT:
            errs.append(f"heavy {h['lifnr']} current month exceeds limit")
    for c in cases:
        e = c["expected"]
        if c.get("_corrupted_by") or "vendor" not in e or "answer" not in e:
            continue
        p = e["period"]
        if w.totals(w.open_items(e["vendor"], p[0], p[1])) != e["answer"]:
            errs.append(f"expected mismatch {c['eval_case_id']}")
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    if a.check:
        wr = {k: read_jsonl(DATA / "world" / f"{k}.jsonl") for k in ["company_codes", "gl_accounts", "vendors", "documents", "gl_balances"]}
        errs = check(wr, read_jsonl(DATA / "eval" / "cases.jsonl"))
        print("\n".join(errs) or "check ok")
        sys.exit(1 if errs else 0)

    rng = random.Random(SEED)
    catalog = yaml.safe_load((DATA / "defects" / "catalog.yaml").read_text(encoding="utf-8"))

    world_raw = build_world(rng)
    counts = {k: write_jsonl(DATA / "world" / f"{k}.jsonl", v) for k, v in world_raw.items()}
    world = World(world_raw)

    cases = build_cases(world, rng)
    cases, clean = corrupt_labels(cases, catalog)
    seen = [{k: v for k, v in c.items() if k != "_corrupted_by"} for c in cases]
    counts["eval_cases"] = write_jsonl(DATA / "eval" / "cases.jsonl", seen)
    write_jsonl(DATA / "ground_truth" / "cases_clean.jsonl", clean)
    write_jsonl(DATA / "ground_truth" / "corrupted.jsonl",
                [{"eval_case_id": c["eval_case_id"], "defect": c["_corrupted_by"]} for c in cases if c.get("_corrupted_by")])

    counts["assets"] = build_healthy()
    variants = build_variants(catalog)
    write_jsonl(DATA / "ground_truth" / "variants.jsonl", variants)
    counts["variants"] = len(variants)

    findings = build_findings(catalog, rng)
    write_jsonl(DATA / "fixtures" / "findings.jsonl", [{k: v for k, v in f.items() if k != "_ground_truth"} for f in findings])
    write_jsonl(DATA / "ground_truth" / "findings.jsonl", findings)
    counts["findings_fixture"] = len(findings)

    by_kind = defaultdict(lambda: {"dev": 0, "holdout": 0})
    for c in cases:
        by_kind[c["kind"]][c["pool"]] += 1
    (DATA / "manifest.json").write_text(json.dumps({
        "world": WORLD_ID, "today": TODAY.isoformat(), "seed": SEED, "counts": counts,
        "cases_by_kind": by_kind, "row_limit": ROW_LIMIT,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    errs = check(world_raw, cases)
    print(json.dumps({"counts": counts, "cases_by_kind": by_kind}, ensure_ascii=False, indent=1))
    print("\n".join(errs) or "check ok")
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
