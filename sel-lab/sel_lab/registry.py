"""로컬 Registry. 자산은 불변 버전 파일이고, 활성 버전은 runs/registry/state.json이 가리킨다.

참조 표기는 기준 문서 형식 `asset://종류/ns/이름@버전` (W0, 2026-10-28). base = 버전을 뺀 `asset://종류/ns/이름`.
- 정상 버전: data/assets/<종류>s/<ns>/<이름>@<버전>.<확장자>
- 결함 버전: data/assets/variants/<종류>s/<ns>/...
- SEL 후보: runs/registry/candidates/<종류>s/<ns>/...

파일 형식
- tool · agent (YAML): kind · metadata{ref, grade} · spec
- skill · prompt (Markdown): front matter{kind, metadata{ref, grade}, description, allowed_tools, role} + 본문
  (skill · prompt 형식은 Protocol P2 Asset에서 확정되면 이 파서만 고친다)

상태(기준 문서 8장 그림 4): draft → candidate → approved, candidate → rejected(평가 미달 · 반려),
릴리스 지정에서 밀려난 approved → deprecated(참조 0건이면 retired. lab은 retired를 쓰지 않음).
"지금 쓰는 버전"은 상태가 아니라 state.json의 active(릴리스 지정 포인터)가 정한다.
롤백하면 deprecated였던 이전 버전이 다시 approved로 표시된다. 그림 4에 없는 전이로, lab의 단순화다.
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
DEFAULT_AGENT = "asset://agent/fi/assistant"
REF_RE = re.compile(r"asset://(\w+)/([\w-]+)/([\w-]+)@(.+)")
BASE_RE = re.compile(r"asset://(\w+)/([\w-]+)/([\w-]+)")


@dataclass
class Asset:
    ref: str
    kind: str
    ns: str
    name: str
    version: str
    meta: dict      # Markdown이면 front matter, YAML이면 문서 전체
    spec: dict      # YAML이면 spec, Markdown이면 {}
    body: str
    text: str
    path: Path

    @property
    def base(self) -> str:
        return f"asset://{self.kind}/{self.ns}/{self.name}"

    @property
    def grade(self):
        return (self.meta.get("metadata") or {}).get("grade")


def split_ref(ref: str):
    """asset://종류/ns/이름@버전 → (종류, ns, 이름, 버전)."""
    m = REF_RE.fullmatch(ref)
    if not m:
        raise ValueError(f"자산 참조 형식 아님: {ref}")
    return m.groups()


def kind_of(ref_or_base: str) -> str | None:
    m = BASE_RE.match(ref_or_base or "")
    return m.group(1) if m else None


def base_of(ref: str) -> str:
    return ref.split("@", 1)[0]


def parse(text: str, path: Path) -> Asset:
    if path.suffix == ".md":
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        meta, body, spec = yaml.safe_load(m.group(1)), m.group(2), {}
    else:
        meta, body = yaml.safe_load(text), ""
        spec = meta.get("spec") or {}
    ref = meta["metadata"]["ref"]
    kind, ns, name, ver = split_ref(ref)
    return Asset(ref, kind, ns, name, ver, meta, spec, body, text, path)


def _dirs(kind: str, ns: str):
    return [DATA / "assets" / FOLDERS[kind] / ns, DATA / "assets" / "variants" / FOLDERS[kind] / ns,
            CAND_DIR / FOLDERS[kind] / ns]


def _candidates_for(kind: str, ns: str, name: str):
    for d in _dirs(kind, ns):
        if d.exists():
            yield from d.glob(f"{name}@*")


def load(ref: str) -> Asset:
    kind, ns, name, ver = split_ref(ref)
    for p in _candidates_for(kind, ns, name):
        if p.stem == f"{name}@{ver}":
            return parse(p.read_text(encoding="utf-8"), p)
    raise FileNotFoundError(ref)


def healthy_path(kind: str, ns: str, name: str) -> Path:
    """정상 버전 파일 (ORACLE 대역이 씀)."""
    return next((DATA / "assets" / FOLDERS[kind] / ns).glob(f"{name}@*"))


def versions(base: str) -> list[str]:
    kind, ns, name = BASE_RE.fullmatch(base).groups()
    return sorted({p.stem.split("@", 1)[1] for p in _candidates_for(kind, ns, name)})


# ---------- 활성 상태 ----------

def _healthy_refs():
    out = {}
    for folder in FOLDERS.values():
        for p in (DATA / "assets" / folder).glob("*/*@*"):
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
    base = base_of(ref)
    ver = ref.split("@", 1)[1]
    prev = st["active"].get(base)
    st["active"][base] = ver
    st["history"].append({"base": base, "from": prev, "to": ver, "reason": reason, "actor": actor})
    st["lifecycle"][ref] = "approved"
    if prev and prev != ver:
        st["lifecycle"][f"{base}@{prev}"] = "deprecated"
    write_state(st)
    return prev


def set_lifecycle(ref: str, status: str) -> None:
    st = read_state()
    st["lifecycle"][ref] = status
    write_state(st)


# ---------- 에이전트 조립 ----------

def resolve_agent(agent_base: str = DEFAULT_AGENT, overrides: dict | None = None):
    """매니페스트(spec.prompt · skills · tools)의 참조를 활성 버전(또는 eval.asset_overrides)으로 풀어 자산 묶음을 돌려준다."""
    agent = load(active_ref(agent_base, overrides))
    spec = agent.spec
    prompt_refs = [spec["prompt"]] if spec.get("prompt") else []
    prompts = [load(active_ref(base_of(r), overrides)) for r in prompt_refs]
    skills = [load(active_ref(base_of(r), overrides)) for r in spec.get("skills", [])]
    tools = [load(active_ref(base_of(r), overrides)) for r in spec.get("tools", [])]
    refs = [agent.ref] + [a.ref for a in prompts + skills + tools]
    return {"agent": agent, "prompts": prompts, "skills": skills, "tools": tools, "refs": refs}


def write_candidate(base_ref: str, new_version: str, text: str) -> Asset:
    kind, ns, name, _ = split_ref(base_ref)
    src = load(base_ref)
    new_ref = f"asset://{kind}/{ns}/{name}@{new_version}"
    text = text.replace(base_ref, new_ref)
    if new_ref not in text:
        text = re.sub(r"(\bref:\s*)\S+", lambda m: m.group(1) + new_ref, text, count=1)
    d = CAND_DIR / FOLDERS[kind] / ns
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}@{new_version}{src.path.suffix}"
    p.write_text(text, encoding="utf-8")
    set_lifecycle(new_ref, "candidate")
    return parse(text, p)
