"""편집 명령 묶음을 기준 자산에 적용한다 (docs/10 W1).

묶음 형식:
    {"base_ref": "asset://…@ver", "change_kind": "skill_body",
     "edits": [{"op": "set", "path": "…:spec.description", "value": "…"},
               {"op": "replace_section", "path": "…#제목/하위", "text": "…"},
               {"op": "append_to_section", "path": "…#제목", "text": "…"}]}

순서: 형식 검증 → 주소 해석 → 허용 주소 대조(하나라도 밖이면 전체 거부) → 적용 → 구조 재검사 → 결과.
실패는 모두 EditError(code, detail)다. 거부 코드는 docs/11 4장.
"""
from __future__ import annotations

import copy
import difflib
import re
from dataclasses import dataclass, field
from typing import Callable

import yaml

from .address import Address, AddressError, parse_address
from .sections import SectionError, block, structure

OPS = {"set", "replace_section", "append_to_section"}
CHANGE_KINDS = {"prompt_instruction", "skill_body", "example", "tool_description", "manifest_param"}
SCALARS = (str, int, float, bool)
SETEXT = re.compile(r"^ {0,3}(=+|-{2,})[ \t]*$")
ATX_LINE = re.compile(r"^ {0,3}#{1,6}([ \t]|$)")


class EditError(Exception):
    def __init__(self, code: str, detail: str, index: int | None = None):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail, self.index = code, detail, index


@dataclass
class EditResult:
    text: str
    diff: str
    changed_lines: int
    addresses: list[str] = field(default_factory=list)


def dump_yaml(d) -> str:
    """생성기(gen/build.py clean)와 같은 직렬화. 바꾼 줄 외에는 서식 차이가 생기지 않는다."""
    return yaml.safe_dump(d, allow_unicode=True, sort_keys=False)


def validate_bundle(b) -> None:
    if not isinstance(b, dict):
        raise EditError("E_SCHEMA", "묶음은 객체여야 한다")
    extra = set(b) - {"base_ref", "change_kind", "edits"}
    if extra:
        raise EditError("E_SCHEMA", f"알 수 없는 키: {sorted(extra)}")
    if not isinstance(b.get("base_ref"), str):
        raise EditError("E_SCHEMA", "base_ref 없음")
    if b.get("change_kind") not in CHANGE_KINDS:
        raise EditError("E_SCHEMA", f"change_kind 열거값 아님: {b.get('change_kind')!r}")
    edits = b.get("edits")
    if not isinstance(edits, list) or not edits:
        raise EditError("E_SCHEMA", "edits는 비어 있지 않은 목록이어야 한다")
    for i, e in enumerate(edits):
        if not isinstance(e, dict) or e.get("op") not in OPS or not isinstance(e.get("path"), str):
            raise EditError("E_SCHEMA", "op · path 형식 위반", i)
        keys = set(e) - {"op", "path"}
        if e["op"] == "set":
            if keys != {"value"} or not isinstance(e["value"], SCALARS) or e["value"] is None:
                raise EditError("E_SCHEMA", "set은 스칼라 value 하나만", i)
        elif keys != {"text"} or not isinstance(e["text"], str):
            raise EditError("E_SCHEMA", f"{e['op']}는 문자열 text 하나만", i)


def _split_md(text: str) -> tuple[str, list[str]]:
    m = re.match(r"^(---\n.*?\n---\n)(.*)$", text, re.S)
    if not m:
        raise EditError("E_ADDR_NOT_FOUND", "front matter가 있는 Markdown이 아님")
    return m.group(1), m.group(2).split("\n")


def _check_inserted(text: str, i: int) -> list[str]:
    lines = text.replace("\r\n", "\n").strip("\n").split("\n")
    for k, line in enumerate(lines):
        if ATX_LINE.match(line):
            raise EditError("E_HEADING_IN_TEXT", f"넣는 글에 제목 줄: {line[:40]!r}", i)
        if SETEXT.match(line) and k > 0 and lines[k - 1].strip():
            raise EditError("E_HEADING_IN_TEXT", f"밑줄형 제목: {line[:40]!r}", i)
    return lines


def _edges(seg: list[str]) -> tuple[int, int]:
    lead = 0
    while lead < len(seg) and seg[lead].strip() == "":
        lead += 1
    trail = 0
    while trail < len(seg) - lead and seg[len(seg) - 1 - trail].strip() == "":
        trail += 1
    return lead, trail


def _apply_md(lines: list[str], addr: Address, op: str, text: str, i: int) -> list[str]:
    new = _check_inserted(text, i)
    try:
        s, e = block(lines, addr.section)
    except SectionError as x:
        raise EditError(x.code, str(x), i)
    seg = lines[s:e]
    lead, trail = _edges(seg)
    core = seg[lead:len(seg) - trail]
    if op == "replace_section":
        core = new
    else:
        core = core + new
    return lines[:s] + seg[:lead] + core + seg[len(seg) - trail:] + lines[e:]


def _apply_yaml(doc: dict, addr: Address, value, i: int) -> None:
    cur = doc
    for k in addr.field[:-1]:
        if not isinstance(cur, dict) or k not in cur:
            raise EditError("E_ADDR_NOT_FOUND", f"필드 없음: {'.'.join(addr.field)}", i)
        cur = cur[k]
    last = addr.field[-1]
    if not isinstance(cur, dict):
        raise EditError("E_ADDR_NOT_FOUND", f"필드 없음: {'.'.join(addr.field)}", i)
    if last not in cur:
        raise EditError("E_NEW_KEY", f"없는 키를 만들 수 없다: {'.'.join(addr.field)}", i)
    old = cur[last]
    if not isinstance(old, SCALARS) or type(old) is not type(value):
        raise EditError("E_TYPE_MISMATCH", f"{type(old).__name__} → {type(value).__name__}: {'.'.join(addr.field)}", i)
    cur[last] = value


def apply_bundle(asset, bundle: dict, allowed: Callable[[object, Address], bool]) -> EditResult:
    """asset: registry.Asset (기준 버전). allowed(asset, addr): 허용 주소 판정 (W2에서 정책 파일로)."""
    validate_bundle(bundle)
    if bundle["base_ref"] != asset.ref:
        raise EditError("E_REF_MISMATCH", f"base_ref {bundle['base_ref']} ≠ 기준 {asset.ref}")
    is_md = asset.path.suffix == ".md"
    addrs = []
    for i, e in enumerate(bundle["edits"]):
        try:
            a = parse_address(e["path"])
        except AddressError as x:
            raise EditError("E_ADDR_PARSE", str(x), i)
        if a.ref != asset.ref:
            raise EditError("E_REF_MISMATCH", f"주소의 자산 · 버전이 기준과 다름: {a.ref}", i)
        if (a.mode == "section") != is_md or (e["op"] == "set") != (a.mode == "field"):
            raise EditError("E_SCHEMA", f"{e['op']}는 이 주소에 쓸 수 없다: {e['path']}", i)
        addrs.append(a)
    for i, a in enumerate(addrs):                     # 적용 전에 전부 대조: 하나라도 밖이면 묶음 전체 거부
        if not allowed(asset, a):
            raise EditError("E_NOT_ALLOWED", f"허용 주소 밖: {a}", i)

    if is_md:
        fm, lines = _split_md(asset.text)
        before = structure(lines)
        for i, (a, e) in enumerate(zip(addrs, bundle["edits"])):
            lines = _apply_md(lines, a, e["op"], e["text"], i)
        if structure(lines) != before:
            raise EditError("E_STRUCTURE_CHANGED", "적용 후 섹션 구조가 달라졌다")
        new_text = fm + "\n".join(lines)
    else:
        doc = copy.deepcopy(asset.meta)
        for i, (a, e) in enumerate(zip(addrs, bundle["edits"])):
            _apply_yaml(doc, a, e["value"], i)
        new_text = dump_yaml(doc)

    if new_text == asset.text:
        raise EditError("E_NO_CHANGE", "적용해도 결과가 같다")
    diff = "\n".join(difflib.unified_diff(asset.text.split("\n"), new_text.split("\n"),
                                         asset.ref, asset.ref + " (edited)", lineterm="", n=1))
    changed = sum(1 for l in diff.split("\n") if l[:1] in "+-" and not l.startswith(("+++", "---")))
    return EditResult(new_text, diff, changed, [str(a) for a in addrs])
