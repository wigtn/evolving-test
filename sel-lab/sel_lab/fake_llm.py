"""오프라인 배관 검증용 규칙 기반 가짜 에이전트 모델.

진짜 LLM이 아니다. 시스템 프롬프트(SKILL·persona)와 도구 설명에 특정 지시가 있으면 따르고, 없으면 틀린다.
그래서 결함 버전 자산을 넣으면 실제로 실패하고, SEL 루프가 그 실패를 잡아 고치는지 끝까지 돌려볼 수 있다.
개선 성능을 재는 용도가 아니다. 실제 평가는 사내 LLM으로 한다.
"""
from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .config import CUR_PERIOD, TODAY

ACCOUNTS = {"가상소모품비": "9510000", "가상운반비": "9520000", "가상외주가공비": "9530000",
            "가상지급수수료": "9540000", "가상수선비": "9550000"}

# 자산 안의 지시를 알아보는 표지 (정상 자산의 문구 일부)
RULES = {
    "period_default": "당월(YYYY-MM)로 조회",
    "disambiguation": "2건 이상이면 후보",
    "amount_format": "천 단위 쉼표",
    "approval": "HITL 승인을 요청",
    "currency_example": "USD 2건 합계",
}


def _fmt(minor: int, waers: str, rule: bool) -> str:
    if waers != "KRW":
        return f"{minor // 100:,}.{minor % 100:02d} {waers}"
    if rule:
        return f"{minor:,}원"
    man = minor / 10_000
    return f"{man:,.0f}만 원" if man >= 1 else f"{minor}원"


def _months(a: str, b: str):
    y, m = map(int, a.split("-"))
    ey, em = map(int, b.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


class RuleFollowingFakeModel(BaseChatModel):
    tool_descriptions: dict = {}

    @property
    def _llm_type(self) -> str:
        return "rule-following-fake"

    def bind_tools(self, tools, **kw):
        descs = {}
        for t in tools:
            if isinstance(t, dict):
                fn = t.get("function", t)
                descs[fn.get("name")] = json.dumps(fn, ensure_ascii=False)
            else:
                descs[getattr(t, "name", "")] = (getattr(t, "description", "") or "") + json.dumps(getattr(t, "args", {}), ensure_ascii=False)
        m = self.model_copy()
        m.tool_descriptions = descs
        return m

    # ---------- 대화 해석 ----------
    def _ctx(self, messages):
        system = "\n".join(m.content for m in messages if isinstance(m, SystemMessage) and isinstance(m.content, str))
        users = [m.content for m in messages if isinstance(m, HumanMessage)]
        calls, results = [], {}
        for m in messages:
            if isinstance(m, AIMessage):
                calls.extend(m.tool_calls or [])
            if isinstance(m, ToolMessage):
                try:
                    results[m.tool_call_id] = json.loads(m.content)
                except Exception:
                    results[m.tool_call_id] = {"raw": m.content}
        rules = {k: (v in system) for k, v in RULES.items()}
        return system, users, calls, results, rules

    def _call(self, name, args):
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:8]}"}])

    def _say(self, text):
        return AIMessage(content=text)

    def _generate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult:
        msg = self._decide(messages)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _decide(self, messages):
        system, users, calls, results, rules = self._ctx(messages)
        first = users[0] if users else ""
        done = [(c, results.get(c["id"])) for c in calls]
        if "전표 올려" in first:
            return self._post_flow(first, done, rules)
        if "잔액" in first:
            return self._gl_flow(first, done)
        return self._open_items_flow(first, users[1:], done, rules)

    # ---------- 미결 조회 ----------
    def _period(self, text, rules):
        if any(k in text for k in ("누적", "연간")):
            return ("2026-01", CUR_PERIOD), "2026-01~" + CUR_PERIOD
        if "전체" in text:
            return ("2025-01", CUR_PERIOD), "전체 기간"
        m = re.search(r"(\d{1,2})월", text)
        if m:
            p = f"{TODAY.year}-{int(m.group(1)):02d}"
            return (p, p), p
        if rules["period_default"]:
            return (CUR_PERIOD, CUR_PERIOD), f"{CUR_PERIOD} 당월 기준"
        return (None, None), "기간 미지정"

    def _open_items_flow(self, first, followups, done, rules):
        names = re.findall(r"가상거래처-\d{4}(?: \S+지점)?", first)
        (p_from, p_to), scope = self._period(first, rules)
        picked, totals, failed = [], {}, []
        i = 0
        for name in names:
            # 1) 거래처 검색
            if i >= len(done):
                return self._call("fi_vendor_search", {"name_query": name})
            rows = (done[i][1] or {}).get("rows", [])
            i += 1
            if not rows:
                failed.append(f"{name}: 거래처 없음")
                continue
            vendor = rows[0]
            if len(rows) > 1:
                reply = " ".join(followups)
                hit = [r for r in rows if r["ORT01"] and r["ORT01"] in reply]
                if hit:
                    vendor = hit[0]
                elif rules["disambiguation"]:
                    cands = ", ".join(f"{r['NAME1']}({r['LIFNR']})" for r in rows)
                    return self._say(f"'{name}'로 찾은 거래처가 여러 곳입니다: {cands}. 어느 거래처인가요?")
            # 2) 미결 조회 (한도 초과면 월 단위로 나눠 재조회)
            chunks = [(p_from, p_to)]
            if i < len(done) and (done[i][1] or {}).get("error") == "ROW_LIMIT_EXCEEDED" and p_from:
                ms = list(_months(p_from, p_to))
                chunks = [(ms[k], ms[min(k + 2, len(ms) - 1)]) for k in range(0, len(ms), 3)]  # 분기 단위
                i += 1
            rows_all, err = [], None
            for cf, ct in chunks:
                if i >= len(done):
                    args = {"lifnr": vendor["LIFNR"], "bukrs": vendor["BUKRS"]}
                    if cf:
                        args.update(period_from=cf, period_to=ct)
                    return self._call("fi_open_items_query", args)
                res = done[i][1] or {}
                i += 1
                if res.get("error"):
                    err = res["error"]
                    break
                rows_all.extend(res.get("rows", []))
            if err:
                failed.append(f"{vendor['NAME1']}: 조회 실패({err})")
                continue
            t = defaultdict(lambda: [0, 0])
            for r in rows_all:
                minor = round(r["WRBTR"] * (1 if r["WAERS"] == "KRW" else 100))
                t[r["WAERS"]][0] += 1
                t[r["WAERS"]][1] += minor
            picked.append(vendor)
            totals[vendor["LIFNR"]] = (vendor, dict(t))
        lines = []
        for lifnr, (v, t) in totals.items():
            if not t:
                lines.append(f"{v['NAME1']}({lifnr}) {scope}: 미결 없음")
            elif rules["currency_example"] or len(t) == 1:
                parts = [f"{w} {c}건 {_fmt(a, w, rules['amount_format'])}" for w, (c, a) in sorted(t.items())]
                lines.append(f"{v['NAME1']}({lifnr}) {scope} 미결: " + ", ".join(parts))
            else:  # 예시가 통화를 합쳐 보여주면 통화를 섞어 합산한다 (D10)
                c = sum(x[0] for x in t.values())
                a = sum(x[1] for x in t.values())
                lines.append(f"{v['NAME1']}({lifnr}) {scope} 미결 {c}건, 합계 {_fmt(a, 'KRW', rules['amount_format'])}")
        lines += failed
        return self._say("\n".join(lines) or "조회할 거래처를 찾지 못했습니다.")

    # ---------- 계정 잔액 ----------
    def _gl_flow(self, first, done):
        m = re.search(r"(9\d{6})", first)
        b = re.search(r"(9[12]00) 회사", first)
        saknr, bukrs = (m.group(1) if m else ""), (b.group(1) if b else "9100")
        desc = self.tool_descriptions.get("fi_gl_balance_query", "")
        knows_format = "YYYY-MM" in desc
        if "지난달" in first:
            raw, norm = "지난달", f"{TODAY.year}-{TODAY.month - 1:02d}"
        else:
            mm = re.search(r"(?:(\d{4})년 )?(\d{1,2})월", first)
            raw = mm.group(0) if mm else ""
            norm = f"{mm.group(1) or TODAY.year}-{int(mm.group(2)):02d}" if mm else ""
        if not done:
            return self._call("fi_gl_balance_query", {"saknr": saknr, "bukrs": bukrs, "period": norm if knows_format else raw})
        res = done[-1][1] or {}
        if res.get("error"):
            return self._say(f"잔액 조회에 실패했습니다 ({res['error']}).")
        r = res["rows"][0]
        return self._say(f"{bukrs} {saknr} {r['PERIOD']} 잔액: {_fmt(int(r['BALANCE']), 'KRW', True)}")

    # ---------- 전표 전기 ----------
    def _post_flow(self, first, done, rules):
        acct = next((s for n, s in ACCOUNTS.items() if n in first), "9510000")
        amt = int(re.search(r"([\d,]+)원", first).group(1).replace(",", ""))
        vname = re.search(r"가상거래처-\d{4}", first).group(0)
        if not done:
            return self._call("fi_vendor_search", {"name_query": vname})
        lifnr = done[0][1]["rows"][0]["LIFNR"]
        lines = [{"saknr": acct, "side": "D", "amount": amt}, {"saknr": "9210000", "side": "C", "amount": amt, "lifnr": lifnr}]
        base = {"bukrs": "9100", "budat": TODAY.isoformat(), "lines": lines}
        if len(done) == 1:
            return self._call("fi_document_post", {**base, "dry_run": True})
        if rules["approval"]:
            if len(done) == 2:
                return self._call("request_approval", {"summary": f"{acct} {amt}원 전표 전기 승인 요청"})
            if len(done) == 3:
                tok = done[2][1].get("approval_token")
                return self._call("fi_document_post", {**base, "dry_run": False, "approval_token": tok})
            return self._say(f"승인 후 전기를 요청했습니다 (결과: {done[3][1].get('status')}).")
        if len(done) == 2:
            return self._call("fi_document_post", {**base, "dry_run": False})
        return self._say(f"전기 요청 결과: {done[2][1].get('status') or done[2][1].get('error')}")
