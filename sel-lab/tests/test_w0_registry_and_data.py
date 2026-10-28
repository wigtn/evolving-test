"""W0 · 레지스트리 · 데이터 생성 · 정적 검사 회귀 테스트."""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from sel_lab import registry, sel, world

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _paths(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_paths(v, f"{prefix}.{k}" if prefix else k))
    else:
        out[prefix] = d
    return out


def _catalog():
    return yaml.safe_load((DATA / "defects" / "catalog.yaml").read_text(encoding="utf-8"))


# ---------- 참조 ----------

@pytest.mark.parametrize("bad", ["skill/fi-open-items@1.4.2", "asset://skill/fi/open-items", "asset://skill/open-items@1.0.0",
                                 "asset://skill/fi/open items@1.0.0", "", "asset:///fi/x@1"])
def test_split_ref_rejects_non_doc_format(bad):
    with pytest.raises(ValueError):
        registry.split_ref(bad)


def test_ref_helpers():
    r = "asset://tool/fi/open-items-query@1.0.0"
    assert registry.split_ref(r) == ("tool", "fi", "open-items-query", "1.0.0")
    assert registry.base_of(r) == "asset://tool/fi/open-items-query"
    assert registry.kind_of(r) == "tool" and registry.kind_of("asset://agent/fi/assistant") == "agent"
    assert registry.kind_of("tool/fi-x") is None


def test_load_unknown_version_raises():
    with pytest.raises(FileNotFoundError):
        registry.load("asset://skill/fi/open-items@9.9.9")


def test_versions_include_healthy_and_variants():
    assert registry.versions("asset://skill/fi/open-items") == ["1.4.2", "1.4.3", "1.4.4", "1.4.5"]


# ---------- 활성 상태 · 후보 ----------

def test_init_state_points_to_healthy_versions():
    st = registry.init_state(force=True)
    assert st["active"]["asset://skill/fi/open-items"] == "1.4.2"
    assert all(k.startswith("asset://") for k in st["active"])


def test_set_active_records_history_and_lifecycle():
    registry.init_state(force=True)
    prev = registry.set_active("asset://skill/fi/open-items@1.4.3", "test", "pytest")
    st = registry.read_state()
    assert prev == "1.4.2" and st["active"]["asset://skill/fi/open-items"] == "1.4.3"
    assert st["lifecycle"]["asset://skill/fi/open-items@1.4.2"] == "retired"
    assert st["history"][-1] == {"base": "asset://skill/fi/open-items", "from": "1.4.2", "to": "1.4.3",
                                 "reason": "test", "actor": "pytest"}
    registry.init_state(force=True)


def test_write_candidate_rewrites_ref_and_location():
    registry.init_state(force=True)
    base = registry.load("asset://tool/fi/gl-balance-query@1.2.0")
    a = registry.write_candidate(base.ref, "1.2.9-test", base.text)
    assert a.ref == "asset://tool/fi/gl-balance-query@1.2.9-test"
    assert a.path.parent == registry.CAND_DIR / "tools" / "fi"
    assert registry.load(a.ref).spec == base.spec
    assert registry.read_state()["lifecycle"][a.ref] == "candidate"


# ---------- 데이터 생성 ----------

def _digest():
    h = hashlib.sha256()
    for p in sorted(DATA.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(DATA)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.mark.slow
def test_build_is_deterministic():
    before = _digest()
    subprocess.run([sys.executable, str(ROOT / "gen" / "build.py")], check=True, cwd=ROOT, capture_output=True)
    assert _digest() == before


def test_loop_assets_have_no_rule_markers():
    for p in (DATA / "assets").rglob("*.md"):
        assert "<!-- rule:" not in p.read_text(encoding="utf-8"), p


def test_every_degraded_version_has_a_variant_file():
    for d in _catalog()["defects"]:
        if d.get("degraded_version"):
            ref = f"{registry.base_of(d['target'])}@{d['degraded_version']}"
            assert registry.load(ref).path.parts[-4] == "variants", ref


def test_variant_changes_only_what_the_defect_says():
    """결함 버전은 정상 버전과 정확히 그 결함만큼만 다르다 (다른 차이가 힌트가 되지 않게)."""
    for d in _catalog()["defects"]:
        if not d.get("degraded_version"):
            continue
        healthy = registry.load(d["target"])
        bad = registry.load(f"{registry.base_of(d['target'])}@{d['degraded_version']}")
        if healthy.path.suffix == ".yaml":
            a, b = _paths(yaml.safe_load(healthy.text)), _paths(yaml.safe_load(bad.text))
            changed = {p for p in set(a) | set(b) if a.get(p) != b.get(p)} - {"metadata.ref"}
            field = d["patch"]["field"]
            pre = (d["patch"].get("also_remove_pattern") or "\0") + "."
            assert field in changed, (d["id"], changed)
            assert all(p == field or p.startswith(pre) for p in changed), (d["id"], changed)
        else:
            a = healthy.body.splitlines()
            b = bad.body.splitlines()
            removed = [l for l in a if l not in b]
            added = [l for l in b if l not in a]
            if d["patch"]["op"] == "remove_rule":
                assert len(removed) == 1 and not added, (d["id"], removed, added)
            else:   # replace_rule: 규칙 줄만 바뀜
                assert 1 <= len(removed) <= 2 and len(added) >= 1, d["id"]


def test_catalog_targets_and_answers_point_to_real_assets():
    for d in _catalog()["defects"]:
        if d["target"] and d["target"].startswith("asset://"):
            registry.load(d["target"])
        addr = d["answer"]["target_address"]
        if addr:
            ref = re.match(r"(asset://[^#:]+?/[^#:]+?/[^#:@]+@[^#:]+)", addr).group(1)
            a = registry.load(ref)
            if "#" in addr:
                sec = addr.split("#", 1)[1]
                heads = [h.strip("# ").strip() for h in a.body.splitlines() if h.startswith("#")]
                assert sec == "" or all(s in heads for s in sec.split("/")), (d["id"], sec, heads)
            else:
                assert addr.split(":", 2)[-1] in _paths(yaml.safe_load(a.text)), d["id"]


# ---------- 조립 · 가짜 세계 ----------

def test_world_tool_table_matches_manifest():
    registry.init_state(force=True)
    b = registry.resolve_agent()
    assert set(world.TOOL_FUNCS) == {t.base for t in b["tools"]}
    for t in b["tools"]:
        assert world.TOOL_FUNCS[t.base][0] == t.spec["name"]


def test_write_tools_declare_dry_run_and_idempotency():
    registry.init_state(force=True)
    for t in registry.resolve_agent()["tools"]:
        if t.spec["side_effect"] == "write":
            assert t.spec["dry_run"] == "supported" and t.spec["idempotency"] == "required", t.ref
            assert t.spec["required_capability"].endswith((".write", ".post")), t.ref


# ---------- 정적 검사 (W2 전 임시 규칙) ----------

def _edit_yaml(a, path, value):
    d = yaml.safe_load(a.text)
    cur = d
    keys = path.split(".")
    for k in keys[:-1]:
        cur = cur[k]
    cur[keys[-1]] = value
    return yaml.safe_dump(d, allow_unicode=True, sort_keys=False)


def test_static_check_accepts_tool_description_edit():
    a = registry.load("asset://tool/fi/gl-balance-query@1.2.0")
    new = _edit_yaml(a, "spec.description", a.spec["description"] + " 예: 2026-09")
    assert sel.static_check(a, new, "metadata") == []


@pytest.mark.parametrize("path,value", [("spec.binding", {"sap_api": "OTHER"}), ("spec.required_capability", "fi.write"),
                                        ("spec.side_effect", "write"), ("spec.name", "renamed"), ("metadata.grade", 1)])
def test_static_check_rejects_forbidden_tool_fields(path, value):
    a = registry.load("asset://tool/fi/gl-balance-query@1.2.0")
    assert sel.static_check(a, _edit_yaml(a, path, value), "metadata")


def test_static_check_agent_only_allowed_field():
    a = registry.load("asset://agent/fi/assistant@1.0.0")
    # W2 정책의 agent params 허용목록은 빈 목록(PM 결정 전) → max_turns도 거부
    assert sel.static_check(a, _edit_yaml(a, "spec.params.max_turns", 16), "composition")
    assert sel.static_check(a, _edit_yaml(a, "spec.tools", a.spec["tools"] + ["asset://tool/fi/x@1.0.0"]), "composition")
    assert sel.static_check(a, _edit_yaml(a, "spec.preset", "inquiry@1"), "composition")


def test_static_check_rejects_front_matter_change():
    a = registry.load("asset://skill/fi/open-items@1.4.2")
    new = a.text.replace("- asset://tool/fi/vendor-search@1.0.0", "- asset://tool/fi/document-post@1.0.0", 1)
    assert new != a.text
    assert any("front matter" in e for e in sel.static_check(a, new, "instruction"))


def test_static_check_rejects_judge_prompt_edit():
    a = registry.load("asset://prompt/fi/judge@1.0.0")
    assert sel.static_check(a, a.text + "\n추가 지시\n", "instruction")


def test_static_check_rejects_no_change():
    a = registry.load("asset://skill/fi/open-items@1.4.2")
    assert "변경 없음" in sel.static_check(a, a.text, "instruction")


# ---------- 결정성 ----------

def _finding(key="k-test"):
    return {"finding_id": "f1", "dedupe_key": key, "signal": "tool_error", "fidelity": "full",
            "target": {"asset_ref": "asset://tool/fi/open-items-query@1.0.0", "channel": "runtime"},
            "hypothesis_class": "description_ambiguity", "evidence": [], "max_grade": 0,
            "metrics": {"support": 30, "distinct_principals": 4, "rate": 0.1, "baseline_rate": 0.02, "reach_weekly": 100}}


def test_case_id_is_deterministic(tmp_path, monkeypatch):
    ids = []
    for i in range(2):
        monkeypatch.setattr(sel, "DB", tmp_path / f"s{i}.sqlite")
        ids.append(sel.ingest([_finding()]))
    assert ids[0] == ids[1] and len(ids[0]) == 1
