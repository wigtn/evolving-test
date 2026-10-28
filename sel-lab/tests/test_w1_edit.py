"""W1 · 편집 대상 주소 · 편집 명령 · 적용기 (docs/10 W1)."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import yaml

from sel_lab import registry, sel
from sel_lab.edit import EditError, apply_bundle, parse_address
from sel_lab.edit.address import Address, AddressError, escape
from sel_lab.governance import allowed as policy_allowed


def allowed(asset, addr):
    """W1 적용기 시험용 허용 판정: 정책 파일(W2) + agent max_turns.
    정책의 agent params는 빈 목록이라, set의 타입 검사를 시험하려고 여기서만 연다."""
    if asset.kind == "agent" and addr.mode == "field":
        return addr.field == ("spec", "params", "max_turns")
    return policy_allowed(asset, addr)
from sel_lab.edit.sections import all_paths, block, headings

ROOT = Path(__file__).resolve().parent.parent
SKILL = "asset://skill/fi/open-items@1.4.3"
TOOL = "asset://tool/fi/gl-balance-query@1.2.1"
AGENT = "asset://agent/fi/assistant@1.0.1"
PERSONA = "asset://prompt/fi/assistant-persona@1.0.1"


def bundle(ref, kind, *edits):
    return {"base_ref": ref, "change_kind": kind, "edits": list(edits)}


def run(ref, kind, *edits):
    return apply_bundle(registry.load(ref), bundle(ref, kind, *edits), allowed)


def code_of(ref, kind, *edits):
    with pytest.raises(EditError) as e:
        run(ref, kind, *edits)
    return e.value.code


# ---------- 주소 ----------

@pytest.mark.parametrize("addr,mode,loc", [
    (f"{TOOL}:spec.description", "field", ("spec", "description")),
    (f"{SKILL}#미결 전표 조회/절차", "section", ("미결 전표 조회", "절차")),
    (f"{PERSONA}#", "section", ()),
    (rf"{SKILL}#a\/b/c\#d\:e\\f", "section", ("a/b", "c#d:e\\f")),
])
def test_parse_address(addr, mode, loc):
    a = parse_address(addr)
    assert a.mode == mode and (a.field if mode == "field" else a.section) == loc
    assert parse_address(str(a)) == a


@pytest.mark.parametrize("bad", ["skill/fi-open-items@1.4.3#절차", f"{SKILL}", f"{TOOL}:spec..description",
                                 f"{TOOL}:", rf"{SKILL}#a\x", f"{SKILL}#a//b", f"{SKILL}#a#b", f"{SKILL}# a", 42])
def test_parse_address_rejects(bad):
    with pytest.raises(AddressError):
        parse_address(bad)


def test_escape_roundtrip():
    for t in ["a/b", "x#y", "p:q", "back\\slash", "평범한 제목"]:
        assert parse_address(f"{SKILL}#{escape(t)}").section == (t,)


def test_every_asset_location_is_addressable():
    for p in (ROOT / "data" / "assets").rglob("*@*"):
        a = registry.parse(p.read_text(encoding="utf-8"), p)
        if p.suffix == ".md":
            lines = a.body.split("\n")
            for path in all_paths(lines):
                block(lines, path)                       # 모든 제목 경로가 하나로 해석된다
                assert parse_address(str(Address(a.ref, "section", section=path))).section == path


def test_code_fence_hash_is_not_heading():
    lines = ["# A", "```", "# not heading", "```", "## B"]
    assert [h.path for h in headings(lines)] == [("A",), ("A", "B")]


# ---------- 적용 ----------

def test_set_tool_description():
    r = run(TOOL, "tool_description", {"op": "set", "path": f"{TOOL}:spec.description", "value": "기간은 YYYY-MM."})
    assert yaml.safe_load(r.text)["spec"]["description"] == "기간은 YYYY-MM." and r.changed_lines == 2


def test_set_argument_description_leaf():
    a = registry.load("asset://tool/fi/gl-balance-query@1.2.0")
    p = f"{a.ref}:spec.input_schema.properties.saknr.description"
    r = apply_bundle(a, bundle(a.ref, "tool_description", {"op": "set", "path": p, "value": "계정 번호 7자리"}), allowed)
    assert yaml.safe_load(r.text)["spec"]["input_schema"]["properties"]["saknr"]["description"] == "계정 번호 7자리"


def test_set_int_param_same_type():
    r = run(AGENT, "manifest_param", {"op": "set", "path": f"{AGENT}:spec.params.max_turns", "value": 12})
    assert yaml.safe_load(r.text)["spec"]["params"]["max_turns"] == 12


def test_replace_section_keeps_neighbours_and_blank_lines():
    a = registry.load(SKILL)
    r = run(SKILL, "skill_body", {"op": "replace_section", "path": f"{SKILL}#미결 전표 조회/절차", "text": "1. 새 절차"})
    old_lines, new_lines = a.body.split("\n"), r.text.split("---\n", 2)[2].split("\n")
    for path in [("미결 전표 조회", "답 형식"), ("미결 전표 조회", "예시")]:
        s1, e1 = block(old_lines, path)
        s2, e2 = block(new_lines, path)
        assert old_lines[s1:e1] == new_lines[s2:e2]
    s, e = block(new_lines, ("미결 전표 조회", "절차"))
    assert new_lines[s:e][0] == "1. 새 절차" and new_lines[e - 1] == ""     # 다음 제목 앞 빈 줄 유지
    assert r.text.split("---\n", 2)[1] == a.text.split("---\n", 2)[1]      # front matter 그대로


def test_replace_parent_section_keeps_subsections():
    r = run(SKILL, "skill_body", {"op": "replace_section", "path": f"{SKILL}#미결 전표 조회", "text": "개요 한 줄"})
    assert "## 절차" in r.text and "## 예시" in r.text and "개요 한 줄" in r.text


def test_append_to_section():
    r = run(SKILL, "skill_body", {"op": "append_to_section", "path": f"{SKILL}#미결 전표 조회/절차", "text": "6. 추가"})
    body = r.text.split("---\n", 2)[2].split("\n")
    s, e = block(body, ("미결 전표 조회", "절차"))
    core = [l for l in body[s:e] if l.strip()]
    assert core[-1] == "6. 추가"


def test_persona_root_section():
    r = run(PERSONA, "prompt_instruction", {"op": "append_to_section", "path": f"{PERSONA}#", "text": "금액은 원 단위 전체 숫자로 쓴다."})
    assert r.text.rstrip().endswith("금액은 원 단위 전체 숫자로 쓴다.")


def test_multiple_edits_in_one_bundle():
    a = registry.load("asset://tool/fi/gl-balance-query@1.2.0")
    r = apply_bundle(a, bundle(a.ref, "tool_description",
                               {"op": "set", "path": f"{a.ref}:spec.description", "value": "설명"},
                               {"op": "set", "path": f"{a.ref}:spec.input_schema.properties.saknr.description", "value": "번호"}), allowed)
    assert len(r.addresses) == 2


def test_apply_is_deterministic():
    e = {"op": "replace_section", "path": f"{SKILL}#미결 전표 조회/절차", "text": "1. 같은 입력"}
    h = {hashlib.sha256(run(SKILL, "skill_body", e).text.encode()).hexdigest() for _ in range(5)}
    assert len(h) == 1


# ---------- 거부 ----------

@pytest.mark.parametrize("b,code", [
    ({"base_ref": SKILL, "change_kind": "skill_body", "edits": []}, "E_SCHEMA"),
    ({"base_ref": SKILL, "change_kind": "rewrite", "edits": [{"op": "append_to_section", "path": f"{SKILL}#", "text": "x"}]}, "E_SCHEMA"),
    ({"base_ref": SKILL, "change_kind": "skill_body", "edits": [{"op": "delete", "path": f"{SKILL}#", "text": "x"}]}, "E_SCHEMA"),
    ({"base_ref": TOOL, "change_kind": "tool_description", "edits": [{"op": "set", "path": f"{TOOL}:spec.description", "value": ["list"]}]}, "E_SCHEMA"),
    ({"base_ref": SKILL, "change_kind": "skill_body", "edits": [{"op": "set", "path": f"{SKILL}#절차", "value": "x"}]}, "E_SCHEMA"),
    ({"base_ref": SKILL, "change_kind": "skill_body", "edits": [{"op": "append_to_section", "path": "bad", "text": "x"}]}, "E_ADDR_PARSE"),
    ({"base_ref": SKILL, "change_kind": "skill_body",
      "edits": [{"op": "append_to_section", "path": "asset://skill/fi/open-items@1.4.2#미결 전표 조회/절차", "text": "x"}]}, "E_REF_MISMATCH"),
    ({"base_ref": "asset://skill/fi/open-items@1.4.2", "change_kind": "skill_body",
      "edits": [{"op": "append_to_section", "path": f"{SKILL}#미결 전표 조회/절차", "text": "x"}]}, "E_REF_MISMATCH"),
])
def test_bundle_rejections(b, code):
    base = registry.load(SKILL if "/skill/" in b["base_ref"] else TOOL)   # 기준 자산은 항상 1.4.3 · 1.2.1
    with pytest.raises(EditError) as e:
        apply_bundle(base, b, allowed)
    assert e.value.code == code


def test_section_not_found_and_ambiguous():
    assert code_of(SKILL, "skill_body", {"op": "append_to_section", "path": f"{SKILL}#없는 섹션", "text": "x"}) == "E_ADDR_NOT_FOUND"
    a = registry.load(SKILL)
    dup = a.text.replace("## 답 형식", "## 절차")
    a2 = registry.parse(dup, a.path)
    with pytest.raises(EditError) as e:
        apply_bundle(a2, bundle(a2.ref, "skill_body", {"op": "append_to_section", "path": f"{SKILL}#미결 전표 조회/절차", "text": "x"}), allowed)
    assert e.value.code == "E_ADDR_AMBIGUOUS"


@pytest.mark.parametrize("path", [":spec.binding.sap_api", ":spec.required_capability", ":spec.name", ":spec.side_effect",
                                  ":spec.input_schema.properties.saknr.pattern", ":spec.input_schema.properties.saknr.type",
                                  ":metadata.grade", ":kind"])
def test_tool_forbidden_addresses(path):
    v = 1 if path.endswith("grade") else "x"
    assert code_of(TOOL, "tool_description", {"op": "set", "path": TOOL + path, "value": v}) == "E_NOT_ALLOWED"


def test_agent_forbidden_and_skill_eval_section_and_judge():
    assert code_of(AGENT, "manifest_param", {"op": "set", "path": f"{AGENT}:spec.preset", "value": "inquiry@1"}) == "E_NOT_ALLOWED"
    judge = registry.load("asset://prompt/fi/judge@1.0.0")
    with pytest.raises(EditError) as e:
        apply_bundle(judge, bundle(judge.ref, "prompt_instruction", {"op": "append_to_section", "path": f"{judge.ref}#", "text": "x"}), allowed)
    assert e.value.code == "E_NOT_ALLOWED"
    assert not allowed(registry.load(SKILL), Address(SKILL, "section", section=("평가 케이스",)))


def test_one_forbidden_edit_rejects_whole_bundle():
    ok = {"op": "set", "path": f"{TOOL}:spec.description", "value": "좋은 설명"}
    bad = {"op": "set", "path": f"{TOOL}:spec.required_capability", "value": "fi.write"}
    assert code_of(TOOL, "tool_description", ok, bad) == "E_NOT_ALLOWED"


@pytest.mark.parametrize("text", ["## 새 섹션\n내용", "# 최상위", "제목\n===", "줄\n---", "   ### 들여쓴 제목"])
def test_heading_injection_rejected(text):
    assert code_of(SKILL, "skill_body", {"op": "append_to_section", "path": f"{SKILL}#미결 전표 조회/절차", "text": text}) == "E_HEADING_IN_TEXT"


def test_unbalanced_fence_that_swallows_headings_is_rejected():
    assert code_of(SKILL, "skill_body", {"op": "append_to_section", "path": f"{SKILL}#미결 전표 조회/절차",
                                         "text": "```"}) == "E_STRUCTURE_CHANGED"


def test_set_type_and_new_key_rejections():
    assert code_of(AGENT, "manifest_param", {"op": "set", "path": f"{AGENT}:spec.params.max_turns", "value": "12"}) == "E_TYPE_MISMATCH"
    assert code_of(AGENT, "manifest_param", {"op": "set", "path": f"{AGENT}:spec.params.max_turns", "value": True}) == "E_TYPE_MISMATCH"
    a = registry.load("asset://tool/fi/gl-balance-query@1.2.0")
    with pytest.raises(EditError) as e:
        apply_bundle(a, bundle(a.ref, "tool_description", {"op": "set", "path": f"{a.ref}:spec.input_schema.properties.nope.description", "value": "x"}), allowed)
    assert e.value.code == "E_ADDR_NOT_FOUND"


def test_no_change_rejected():
    a = registry.load(TOOL)
    assert code_of(TOOL, "tool_description", {"op": "set", "path": f"{TOOL}:spec.description", "value": a.spec["description"]}) == "E_NO_CHANGE"


# ---------- ORACLE ----------

def _catalog():
    return yaml.safe_load((ROOT / "data" / "defects" / "catalog.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("did", ["D01", "D02", "D03", "D04", "D05", "D06", "D10"])
def test_oracle_edits_hit_catalog_answer_address(did):
    d = next(x for x in _catalog()["defects"] if x["id"] == did)
    src = registry.load(f"{registry.base_of(d['target'])}@{d['degraded_version']}")
    out = sel._improver_oracle(src, d["expect"]["change_type"])
    assert {e["path"] for e in out["edits"]["edits"]} == {d["answer"]["target_address"]}
    assert out["edits"]["change_kind"] == d["answer"]["change_kind"]
    res = apply_bundle(src, out["edits"], allowed)
    healthy = registry.load(d["target"])
    if src.path.suffix == ".md":                  # 마크다운은 정상 버전 본문으로 정확히 돌아간다
        assert res.text.split("---\n", 2)[2] == healthy.text.split("---\n", 2)[2]


def test_oracle_does_not_restore_forbidden_pattern_for_d06():
    src = registry.load("asset://tool/fi/gl-balance-query@1.2.1")
    paths = {e["path"] for e in sel._improver_oracle(src, "metadata")["edits"]["edits"]}
    assert not any(p.endswith(".pattern") for p in paths)
