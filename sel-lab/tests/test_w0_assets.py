"""W0 · 자산 형식 이관 검사 (docs/10 W0)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sel_lab import registry  # noqa: E402

REF_RE = re.compile(r"asset://(\w+)/([\w-]+)/([\w-]+)@(.+)")
OLD_REF = re.compile(r"(?<![\w/:-])(skill|tool|agent|prompt)/fi-[a-z-]+@")
C6 = {"proceed", "handoff_dev", "undiagnosed", "insufficient_set", "input_unavailable", "out_of_scope",
      "registered", "governance_rejected", "generation_failed", "attempts_exhausted"}
CAUSE = {"asset", "tool_binding", "code", "infra", "unknown"}
CHANGE = {None, "prompt_instruction", "skill_body", "example", "tool_description", "manifest_param"}


def asset_files():
    for sub in ("assets", "assets_src"):
        yield from (p for p in (ROOT / "data" / sub).rglob("*@*") if p.is_file())


def test_all_assets_parse_and_live_where_their_ref_says():
    files = list(asset_files())
    assert files
    for p in files:
        if "assets_src" in p.parts:
            text = re.sub(r"^<!-- rule:[\w_]+ -->\n", "", p.read_text(encoding="utf-8"), flags=re.M)
        else:
            text = p.read_text(encoding="utf-8")
        a = registry.parse(text, p)
        kind, ns, name, ver = REF_RE.fullmatch(a.ref).groups()
        assert p.name == f"{name}@{ver}{p.suffix}", p
        assert p.parent.name == ns and p.parent.parent.name == registry.FOLDERS[kind], p
        assert a.grade in (1, 2, 3), p


def test_tool_and_agent_follow_doc_structure():
    for p in (ROOT / "data" / "assets").rglob("*.yaml"):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert set(d) == {"kind", "metadata", "spec"}, p
        if d["kind"] == "tool":
            for k in ("name", "description", "input_schema", "side_effect", "required_capability", "dry_run",
                      "idempotency", "timeout_s", "binding"):
                assert k in d["spec"], (p, k)
        if d["kind"] == "agent":
            for k in ("preset", "prompt", "skills", "tools", "extensions", "params"):
                assert k in d["spec"], (p, k)


def test_no_old_style_refs_left():
    hits = []
    for base in ("data", "sel_lab", "gen"):
        for p in (ROOT / base).rglob("*"):
            if p.is_file() and p.suffix in {".py", ".yaml", ".md", ".json", ".jsonl"} and p.name != "migrate_w0.py":
                if OLD_REF.search(p.read_text(encoding="utf-8", errors="ignore")):
                    hits.append(str(p.relative_to(ROOT)))
    assert not hits, hits


def test_manifest_refs_resolve():
    registry.init_state(force=True)
    b = registry.resolve_agent()
    assert b["agent"].ref == "asset://agent/fi/assistant@1.0.0"
    assert [t.spec["name"] for t in b["tools"]] == ["fi_vendor_search", "fi_open_items_query", "fi_gl_balance_query",
                                                     "fi_document_post"]
    skill_tools = {t for s in b["skills"] for t in s.meta["allowed_tools"]}
    assert skill_tools <= set(b["agent"].spec["tools"])      # 스킬별 허용 도구는 매니페스트 도구의 부분집합


def test_catalog_answers_present_and_valid():
    cat = yaml.safe_load((ROOT / "data" / "defects" / "catalog.yaml").read_text(encoding="utf-8"))
    for d in cat["defects"]:
        a = d["answer"]
        assert a["cause_class"] in CAUSE, d["id"]
        assert a["change_kind"] in CHANGE, d["id"]
        assert a["c6"] and set(a["c6"]) <= C6, d["id"]
        if a["target_address"]:
            ref = re.split(r"[#:](?!//)", a["target_address"].replace("asset://", "asset§", 1), maxsplit=1)[0].replace("asset§", "asset://")
            assert REF_RE.fullmatch(ref), (d["id"], ref)


@pytest.mark.slow
def test_demo_snapshot_unchanged(tmp_path):
    """오프라인 demo 결과가 고정된 스냅샷(tests/snapshots/demo.json)과 같다. 몇 분 걸린다: pytest -m slow"""
    out = tmp_path / "snap.json"
    subprocess.run([sys.executable, str(ROOT / "tests" / "snapshot_demo.py"), str(out)], check=True, cwd=ROOT,
                   env={**os.environ, "SEL_OFFLINE": "1"}, capture_output=True)
    assert json.loads(out.read_text()) == json.loads((ROOT / "tests" / "snapshots" / "demo.json").read_text())
