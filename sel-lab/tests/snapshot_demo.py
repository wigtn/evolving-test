"""W0-5 스냅샷: 오프라인 demo를 결함마다 돌려 결과를 참조 형식과 무관한 JSON으로 저장한다.

python tests/snapshot_demo.py out.json
같은 설정으로 이관 전 · 후를 돌려 두 파일이 같은지 본다.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys

os.environ["SEL_OFFLINE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sel_lab import cli, registry, sel  # noqa: E402

# 옛 참조 → 기준 문서 형식. 이관 뒤에는 그대로 통과한다.
OLD2NEW = {
    "agent/fi-assistant": "asset://agent/fi/assistant",
    "prompt/fi-assistant-persona": "asset://prompt/fi/assistant-persona",
    "prompt/fi-judge": "asset://prompt/fi/judge",
    "skill/fi-open-items": "asset://skill/fi/open-items",
    "skill/fi-gl-balance": "asset://skill/fi/gl-balance",
    "skill/fi-document-post": "asset://skill/fi/document-post",
    "tool/fi-open-items-query": "asset://tool/fi/open-items-query",
    "tool/fi-gl-balance-query": "asset://tool/fi/gl-balance-query",
    "tool/fi-vendor-search": "asset://tool/fi/vendor-search",
    "tool/fi-document-post": "asset://tool/fi/document-post",
}


def norm(s):
    if not isinstance(s, str):
        return s
    if s.startswith("asset://"):
        return s
    for o, n in sorted(OLD2NEW.items(), key=lambda kv: -len(kv[0])):
        s = re.sub(rf"(?<![\w/:-]){re.escape(o)}(?=@|$)", n, s)
    return s


RUNS = {
    "D01": dict(n=250), "D02": dict(n=250, focus="open_items_ambiguous:8"), "D03": dict(n=250),
    "D04": dict(n=250), "D05": dict(n=250), "D06": dict(n=250), "D07": dict(n=250),
    "D08": dict(n=250), "D09": dict(n=250, recon=12), "D10": dict(n=250),
}


def one(did, opts):
    ns = argparse.Namespace(defect=did, n=opts["n"], k=3, recon=opts.get("recon", 0), auto_approve=True,
                            focus=opts.get("focus"))
    buf = io.StringIO()
    err = None
    with contextlib.redirect_stdout(buf):
        try:
            cli.cmd_demo(ns)
        except SystemExit as e:
            err = f"SystemExit:{e}"
        except Exception as e:  # 스냅샷에 그대로 남긴다
            err = f"{type(e).__name__}:{e}"
    cases = []
    for c in sel.list_cases():
        case, _ = sel.show(c["case_id"])
        b = case["body"]
        h = b.get("hypothesis") or {}
        g = b.get("gate") or {}
        cases.append({"signal": c["signal"], "state": c["state"], "target": norm(h.get("target_asset_ref")),
                      "change": h.get("change_type"), "gate": g.get("verdict"),
                      "verification": b.get("verification")})
    st = registry.read_state()
    active = {norm(k): v for k, v in st["active"].items()}
    return {"error": err, "cases": sorted(cases, key=lambda x: (x["signal"], x["target"] or "")), "active": active}


def main():
    out = {}
    only = sys.argv[2:] or list(RUNS)
    for did in only:
        out[did] = one(did, RUNS[did])
        print(did, out[did]["error"] or "", [(c["signal"], c["state"]) for c in out[did]["cases"]], file=sys.stderr)
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
