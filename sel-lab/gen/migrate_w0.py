"""W0 일회성 이관: data/assets_src의 자산을 기준 문서 형식으로 바꾼다 (한 번 실행한 기록으로 남긴다).

- 참조: 종류/fi-이름@버전 → asset://종류/fi/이름@버전
- tool · agent: kind · metadata · spec 구조 (기준 문서 6 · 9장 예시)
- skill · prompt: Markdown 본문과 front matter를 유지하고 kind · metadata만 덧붙인다 (형식은 Protocol P2에서 확정)
- 파일 위치: data/assets_src/<종류>s/<ns>/<이름>@<버전>.<확장자>

python gen/migrate_w0.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "assets_src"
FOLDERS = {"skill": "skills", "prompt": "prompts", "tool": "tools", "agent": "agents"}
NS = "fi"
TOOL_NAMES = {"open-items-query": "fi_open_items_query", "gl-balance-query": "fi_gl_balance_query",
              "vendor-search": "fi_vendor_search", "document-post": "fi_document_post"}
GRADE = {("tool", "document-post"): 2, ("skill", "document-post"): 2}   # 쓰기 업무는 한 단계 더 민감하게 (합성 값, Q8 전 가정)


def new_ref(old: str) -> str:
    kind, rest = old.split("/", 1)
    name, ver = rest.split("@", 1)
    return f"asset://{kind}/{NS}/{name.removeprefix('fi-')}@{ver}"


def split_new(ref: str):
    m = re.fullmatch(r"asset://(\w+)/([\w-]+)/([\w-]+)@(.+)", ref)
    return m.groups()


def dump(d) -> str:
    return yaml.safe_dump(d, allow_unicode=True, sort_keys=False)


def conv_tool(old: dict) -> dict:
    ref = new_ref(old["asset_ref"])
    _, _, name, _ = split_new(ref)
    write = old.get("mode") == "write"
    spec = {"name": TOOL_NAMES[name], "description": old["description"], "input_schema": old["input_schema"],
            "side_effect": old.get("mode", "read"), "required_capability": old.get("required_role"),
            "dry_run": old.get("dry_run", "unsupported"), "idempotency": "required" if write else "none",
            "timeout_s": 20, "binding": {"sap_api": f"SIM_{TOOL_NAMES[name].upper()}"}}
    for k in ("row_limit", "errors", "output"):           # lab 확장 필드 (가짜 세계가 씀)
        if k in old:
            spec[k] = old[k]
    return {"kind": "tool", "metadata": {"ref": ref, "grade": GRADE.get(("tool", name), 3)}, "spec": spec}


def conv_agent(old: dict, skills_tools: dict) -> dict:
    ref = new_ref(old["asset_ref"])
    skills = [new_ref(s) for s in old["skills"]]
    tools = []
    for s in old["skills"]:                                  # 기존 조립 순서와 같게: 스킬 순서 × 스킬의 허용 도구 순서
        for t in skills_tools[s.split("@")[0]]:
            if t not in tools:
                tools.append(t)
    spec = {"preset": "transaction@1", "prompt": new_ref(old["prompts"][0]), "skills": skills, "tools": tools,
            "extensions": [], "params": {"max_turns": old.get("max_turns", 12)},
            "runtime": old.get("runtime", "deepagents"), "model": old.get("model")}
    return {"kind": "agent", "metadata": {"ref": ref, "grade": 2}, "spec": spec}


def conv_md(text: str):
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    fm, body = yaml.safe_load(m.group(1)), m.group(2)
    ref = new_ref(fm["asset_ref"])
    kind, _, name, _ = split_new(ref)
    out = {"kind": fm["kind"], "metadata": {"ref": ref, "grade": GRADE.get((kind, name), 3)}}
    if "description" in fm:
        out["description"] = fm["description"]
    if "allowed_tools" in fm:
        out["allowed_tools"] = [new_ref(t) for t in fm["allowed_tools"]]
    if "role" in fm:
        out["role"] = fm["role"]
    return "---\n" + dump(out) + "---\n" + body


def main():
    files = sorted(p for p in SRC.glob("*/*@*") if p.is_file())
    if not files:
        print("이미 이관됨")
        return
    skills_tools = {}
    for p in files:
        if p.parent.name == "skills":
            fm = yaml.safe_load(re.match(r"^---\n(.*?)\n---", p.read_text(encoding="utf-8"), re.S).group(1))
            skills_tools[fm["asset_ref"].split("@")[0]] = [new_ref(t) for t in fm["allowed_tools"]]
    for p in files:
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".md":
            out = conv_md(text)
            ref = new_ref(yaml.safe_load(re.match(r"^---\n(.*?)\n---", text, re.S).group(1))["asset_ref"])
        else:
            old = yaml.safe_load(text)
            d = conv_tool(old) if old["kind"] == "tool" else conv_agent(old, skills_tools)
            out, ref = dump(d), d["metadata"]["ref"]
        kind, ns, name, ver = split_new(ref)
        dst = SRC / FOLDERS[kind] / ns / f"{name}@{ver}{p.suffix}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(out, encoding="utf-8")
        p.unlink()
        print(f"{p.relative_to(ROOT)} → {dst.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
