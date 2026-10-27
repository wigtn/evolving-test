"""로컬 Registry. 자산은 불변 버전 파일이고, 활성 버전은 runs/registry/state.json이 가리킨다.

참조 표기는 종류/이름@버전. 정상 버전은 data/assets/<kind>s/, 결함 버전은 data/assets/variants/,
SEL 후보는 runs/registry/candidates/에 있다. 상태: draft → candidate → approved → active → retired.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import DATA, RUNS

FOLDERS = {"skill": "skills", "prompt": "prompts", "tool": "tools", "agent": "agents"}
STATE = RUNS / "registry" / "state.json"
CAND_DIR = RUNS / "registry" / "candidates"
DEFAULT_AGENT = "agent/fi-assistant"


@dataclass
class Asset:
    ref: str
    kind: str
    name: str
    version: str
    meta: dict
    body: str
    text: str
    path: Path

    @property
    def base(self) -> str:
        return f"{self.kind}/{self.name}"


def split_ref(ref: str):
    base, ver = ref.split("@", 1)
    kind, name = base.split("/", 1)
    return kind, name, ver


def parse(text: str, path: Path) -> Asset:
    if path.suffix == ".md":
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        meta, body = yaml.safe_load(m.group(1)), m.group(2)
    else:
        meta, body = yaml.safe_load(text), ""
    kind, name, ver = split_ref(meta["asset_ref"])
    return Asset(meta["asset_ref"], kind, name, ver, meta, body, text, path)


def _candidates_for(kind: str, name: str):
    dirs = [DATA / "assets" / FOLDERS[kind], DATA / "assets" / "variants", CAND_DIR]
    for d in dirs:
        if d.exists():
            for p in d.glob(f"{name}@*"):
                yield p


def load(ref: str) -> Asset:
    kind, name, ver = split_ref(ref)
    for p in _candidates_for(kind, name):
        if p.stem == f"{name}@{ver}":
            return parse(p.read_text(encoding="utf-8"), p)
    raise FileNotFoundError(ref)


def versions(base: str) -> list[str]:
    kind, name = base.split("/", 1)
    return sorted({p.stem.split("@", 1)[1] for p in _candidates_for(kind, name)})


# ---------- 활성 상태 ----------

def _healthy_refs():
    out = {}
    for kind, folder in FOLDERS.items():
        for p in (DATA / "assets" / folder).glob("*@*"):
            a = parse(p.read_text(encoding="utf-8"), p)
            out[a.base] = a.version
    return out


def init_state(force: bool = False) -> dict:
    if STATE.exists() and not force:
        return read_state()
    st = {"active": _healthy_refs(), "faults": {}, "history": [], "lifecycle": {}}
    write_state(st)
    return st


def read_state() -> dict:
    if not STATE.exists():
        return init_state()
    return json.loads(STATE.read_text(encoding="utf-8"))


def write_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def active_ref(base: str, overrides: dict | None = None) -> str:
    if overrides and base in overrides:
        return f"{base}@{overrides[base]}"
    return f"{base}@{read_state()['active'][base]}"


def set_active(ref: str, reason: str, actor: str) -> str | None:
    st = read_state()
    kind, name, ver = split_ref(ref)
    base = f"{kind}/{name}"
    prev = st["active"].get(base)
    st["active"][base] = ver
    st["history"].append({"base": base, "from": prev, "to": ver, "reason": reason, "actor": actor})
    st["lifecycle"][ref] = "active"
    if prev:
        st["lifecycle"][f"{base}@{prev}"] = "retired"
    write_state(st)
    return prev


def set_lifecycle(ref: str, status: str) -> None:
    st = read_state()
    st["lifecycle"][ref] = status
    write_state(st)


# ---------- 에이전트 조립 ----------

def resolve_agent(agent_base: str = DEFAULT_AGENT, overrides: dict | None = None):
    """에이전트 매니페스트의 참조를 활성 버전(또는 eval.asset_overrides)으로 풀어 자산 묶음을 돌려준다."""
    agent = load(active_ref(agent_base, overrides))
    prompts = [load(active_ref(r.split("@")[0], overrides)) for r in agent.meta.get("prompts", [])]
    skills = [load(active_ref(r.split("@")[0], overrides)) for r in agent.meta.get("skills", [])]
    tool_bases = []
    for s in skills:
        for t in s.meta.get("allowed_tools", []):
            b = t.split("@")[0]
            if b not in tool_bases:
                tool_bases.append(b)
    tools = [load(active_ref(b, overrides)) for b in tool_bases]
    refs = [agent.ref] + [a.ref for a in prompts + skills + tools]
    return {"agent": agent, "prompts": prompts, "skills": skills, "tools": tools, "refs": refs}


def write_candidate(base_ref: str, new_version: str, text: str) -> Asset:
    kind, name, _ = split_ref(base_ref)
    src = load(base_ref)
    new_ref = f"{kind}/{name}@{new_version}"
    text = text.replace(base_ref, new_ref)
    if new_ref not in text:
        text = re.sub(r"asset_ref:\s*\S+", f"asset_ref: {new_ref}", text, count=1)
    CAND_DIR.mkdir(parents=True, exist_ok=True)
    p = CAND_DIR / f"{name}@{new_version}{src.path.suffix}"
    p.write_text(text, encoding="utf-8")
    set_lifecycle(new_ref, "candidate")
    return parse(text, p)
