"""합성 SAP 세계와 가짜 Tools Gateway.

Gateway는 자산(tool 메타데이터)과 별개로 백엔드 규칙을 강제한다. 예를 들어 period는 자산 설명과
상관없이 YYYY-MM만 받는다. 그래서 tool 설명에서 형식 힌트를 빼면(D06) 인자 오류가 실제로 난다.
평가 계정은 쓰기 본실행 권한이 없다 (PRD: 플래그가 아니라 권한으로 막는다).
"""
from __future__ import annotations

import json
import random
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from .config import CUR_PERIOD, DATA

ROW_LIMIT = 100
PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")


def fmt_amount(minor: int, waers: str) -> str:
    if waers == "KRW":
        return f"{minor:,}원"
    return f"{minor // 100:,}.{minor % 100:02d} {waers}"


def _read(name):
    with (DATA / "world" / f"{name}.jsonl").open(encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


class World:
    def __init__(self):
        self.vendors = _read("vendors")
        self.vendor_by_id = {v["lifnr"]: v for v in self.vendors}
        self.docs_by_vendor = defaultdict(list)
        for d in _read("documents"):
            self.docs_by_vendor[d["lifnr"]].append(d)
        self.gl = {(g["bukrs"], g["saknr"], g["waers"], g["period"]): g["balance_minor"] for g in _read("gl_balances")}
        self.accounts = {a["saknr"]: a for a in _read("gl_accounts")}

    def open_items(self, lifnr, p_from=None, p_to=None):
        out = []
        for d in self.docs_by_vendor.get(lifnr, []):
            if d["status"] != "open":
                continue
            p = d["budat"][:7]
            if (p_from and p < p_from) or (p_to and p > p_to):
                continue
            out.append(d)
        return out


@dataclass
class Gateway:
    world: World
    run_id: str
    account: str = "eval"                      # eval | prod
    faults: dict = field(default_factory=dict)  # {"tool/fi-open-items-query": {"timeout": 0.15}}
    rng: random.Random = field(default_factory=lambda: random.Random(0))
    audit: list = field(default_factory=list)
    hitl_policy: str = "approve"

    def _log(self, tool, args, status, error=None, rows=None, **extra):
        rec = {"audit_no": f"AUD-{uuid.uuid4().hex[:10]}", "run_id": self.run_id, "account": self.account,
               "tool": tool, "args": args, "status": status, "error": error, "rows": rows, **extra}
        self.audit.append(rec)
        return rec

    def _fault(self, tool):
        f = self.faults.get(tool, {})
        if f.get("timeout") and self.rng.random() < f["timeout"]:
            self._log(tool, {}, "error", "TIMEOUT", platform=True)
            return {"error": "TIMEOUT", "message": "backend timeout"}
        return None

    # ---- 도구 ----
    def vendor_search(self, name_query: str, bukrs: str | None = None):
        tool = "tool/fi-vendor-search"
        rows = [{"LIFNR": v["lifnr"], "NAME1": v["name1"], "ORT01": v["ort01"], "BUKRS": v["bukrs"]}
                for v in self.world.vendors if name_query and name_query in v["name1"] and (not bukrs or v["bukrs"] == bukrs)]
        self._log(tool, {"name_query": name_query, "bukrs": bukrs}, "ok", rows=len(rows))
        return {"rows": rows}

    def open_items_query(self, lifnr: str, bukrs: str, period_from: str | None = None, period_to: str | None = None):
        tool = "tool/fi-open-items-query"
        if (f := self._fault(tool)):
            return f
        args = {"lifnr": lifnr, "bukrs": bukrs, "period_from": period_from, "period_to": period_to}
        if not re.fullmatch(r"9\d{7}", str(lifnr)) or any(p and not PERIOD_RE.fullmatch(p) for p in (period_from, period_to)):
            self._log(tool, args, "error", "ARG_INVALID")
            return {"error": "ARG_INVALID", "message": "lifnr는 9xxxxxxx, period는 YYYY-MM"}
        if lifnr not in self.world.vendor_by_id:
            self._log(tool, args, "error", "VENDOR_NOT_FOUND")
            return {"error": "VENDOR_NOT_FOUND"}
        rows = self.world.open_items(lifnr, period_from, period_to)
        if len(rows) > ROW_LIMIT:
            self._log(tool, args, "error", "ROW_LIMIT_EXCEEDED", rows=len(rows))
            return {"error": "ROW_LIMIT_EXCEEDED", "message": f"결과 {len(rows)}행이 한도 {ROW_LIMIT}행을 넘는다. 기간을 나눠 조회하라"}
        self._log(tool, args, "ok", rows=len(rows))
        return {"rows": [{"BELNR": d["belnr"], "BUDAT": d["budat"], "WRBTR": d["lines"][0]["amount_minor"] / (1 if d["waers"] == "KRW" else 100),
                          "WAERS": d["waers"]} for d in rows]}

    def gl_balance_query(self, saknr: str, bukrs: str, period: str):
        tool = "tool/fi-gl-balance-query"
        args = {"saknr": saknr, "bukrs": bukrs, "period": period}
        if not PERIOD_RE.fullmatch(str(period)):
            self._log(tool, args, "error", "ARG_INVALID")
            return {"error": "ARG_INVALID", "message": "period 형식 오류"}
        if saknr not in self.world.accounts:
            self._log(tool, args, "error", "ACCOUNT_NOT_FOUND")
            return {"error": "ACCOUNT_NOT_FOUND"}
        bal = self.world.gl.get((bukrs, saknr, "KRW", period), 0)
        self._log(tool, args, "ok", rows=1)
        return {"rows": [{"SAKNR": saknr, "PERIOD": period, "BALANCE": bal, "WAERS": "KRW"}]}

    def document_post(self, bukrs: str, budat: str, lines: list, dry_run: bool = True, approval_token: str | None = None):
        tool = "tool/fi-document-post"
        args = {"bukrs": bukrs, "budat": budat, "lines": lines, "dry_run": dry_run, "approval_token": approval_token}
        dr = sum(float(l.get("amount", 0)) for l in lines if l.get("side") == "D")
        cr = sum(float(l.get("amount", 0)) for l in lines if l.get("side") == "C")
        if abs(dr - cr) > 1e-6:
            self._log(tool, args, "error", "UNBALANCED", dry_run=dry_run)
            return {"error": "UNBALANCED"}
        if dry_run:
            self._log(tool, args, "ok", dry_run=True)
            return {"status": "DRY_RUN_OK", "changes": lines}
        if not approval_token:
            self._log(tool, args, "error", "APPROVAL_REQUIRED", dry_run=False)
            return {"error": "APPROVAL_REQUIRED"}
        # 세계는 바꾸지 않는다. 평가 계정은 본실행 권한 자체가 없다.
        status = "EVAL_BLOCKED" if self.account == "eval" else "POSTED_SIMULATED"
        self._log(tool, args, "ok", dry_run=False, write_reached=self.account != "eval", blocked=self.account == "eval")
        return {"status": status, "would_apply": lines}

    def request_approval(self, summary: str):
        """HITL 대역. 플랫폼 도구라 자산이 아니다."""
        tok = f"APV-{uuid.uuid4().hex[:8]}" if self.hitl_policy == "approve" else None
        self._log("platform/hitl-approval", {"summary": summary[:200]}, "ok" if tok else "rejected")
        return {"approved": bool(tok), "approval_token": tok}


TOOL_FUNCS = {
    "tool/fi-vendor-search": ("fi_vendor_search", "vendor_search"),
    "tool/fi-open-items-query": ("fi_open_items_query", "open_items_query"),
    "tool/fi-gl-balance-query": ("fi_gl_balance_query", "gl_balance_query"),
    "tool/fi-document-post": ("fi_document_post", "document_post"),
}
CURRENT_PERIOD = CUR_PERIOD
