"""거버넌스 정책 파일 로더 (docs/10 W2-1 · W2-2).

- 모르는 schema_version · 키 · 형식은 PolicyError (fail-closed).
- 내용 해시 = 파일 바이트의 sha256. 생성 근거 · 판정 로그에 ref와 함께 남긴다.
- 정책 파일은 runs/ (SEL이 쓰는 곳) 안에 있으면 거부한다.
- allowed(asset, addr): 편집 적용기(W1 apply_bundle)에 넘기는 허용 주소 판정.
"""
from __future__ import annotations

import functools
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import DATA, RUNS

DEFAULT_PATH = DATA / "policy" / "sel-governance-policy@0.1.0.yaml"
SCHEMA_VERSIONS = {1}
CHECKS = ["scope", "size", "inflow", "safety", "leak", "grade"]
ASSET_KINDS = {"skill", "prompt", "tool", "agent"}

# 키 스키마: dict는 하위 스키마, type은 값 타입. 여기 없는 키는 거부한다.
_STR_LIST = ("list", str)
SCHEMA = {
    "schema_version": int, "ref": str, "default": str, "checks": _STR_LIST,
    "change_kinds": ("map", str),
    "allow": {
        "tool": {"fields": _STR_LIST},
        "agent": {"params": _STR_LIST},
        "skill": {"deny_sections": _STR_LIST, "example_sections": _STR_LIST},
        "prompt": {"deny_roles": _STR_LIST},
    },
    "limits": {"edits_per_bundle": int, "section_changed_lines": int, "description_chars": int,
               "arg_description_chars": int},
    "inflow": {"evidence_ngram": int, "evidence_containment": float, "directive_patterns": _STR_LIST},
    "safety": {"keywords": _STR_LIST, "weakening_phrases": _STR_LIST, "conditional_phrases": _STR_LIST, "window": int,
               "strong_markers": _STR_LIST},
    "leak": {"copy_ngram": int, "identifiers": ("list", dict), "exempt": {"dates": bool, "amount_units": _STR_LIST}},
    "synthetic": {"name_prefixes": _STR_LIST, "reserved_numbers": ("list", dict)},
    "grade": {"most_sensitive": str, "unknown": str},
}
IDENT_KEYS = {"id", "name", "pattern", "min_digits"}
RESERVED_KEYS = {"name", "pattern"}


class PolicyError(Exception):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    """같은 키가 두 번 나오면 거부한다(PyYAML 기본은 뒤의 값으로 조용히 덮어써 검사를 끌 수 있다)."""


def _no_dup(loader, node, deep=False):
    keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
    dup = {k for k in keys if keys.count(k) > 1}
    if dup:
        raise PolicyError(f"중복 키: {sorted(map(str, dup))}")
    return loader.construct_mapping(node, deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup)

# 값 범위. 사전이 비거나 한도가 0이면 검사가 꺼진 것과 같으므로 거부한다.
RANGES = {("limits", "edits_per_bundle"): (1, 50), ("limits", "section_changed_lines"): (1, 500),
          ("limits", "description_chars"): (1, 10000), ("limits", "arg_description_chars"): (1, 5000),
          ("inflow", "evidence_ngram"): (3, 50), ("inflow", "evidence_containment"): (0.05, 1.0),
          ("safety", "window"): (5, 500), ("leak", "copy_ngram"): (8, 200)}
NONEMPTY = [("checks",), ("change_kinds",), ("inflow", "directive_patterns"), ("safety", "keywords"),
            ("safety", "weakening_phrases"), ("safety", "strong_markers"), ("leak", "identifiers"),
            ("synthetic", "reserved_numbers")]


def _validate(node, schema, where: str):
    if isinstance(schema, dict):
        if not isinstance(node, dict):
            raise PolicyError(f"{where}: 객체여야 한다")
        unknown = set(node) - set(schema)
        if unknown:
            raise PolicyError(f"{where}: 모르는 키 {sorted(unknown)}")
        missing = set(schema) - set(node)
        if missing:
            raise PolicyError(f"{where}: 빠진 키 {sorted(missing)}")
        for k, sub in schema.items():
            _validate(node[k], sub, f"{where}.{k}" if where else k)
    elif isinstance(schema, tuple):
        shape, t = schema
        if shape == "list":
            if not isinstance(node, list) or not all(isinstance(x, t) for x in node):
                raise PolicyError(f"{where}: {t.__name__} 목록이어야 한다")
        else:
            if not isinstance(node, dict) or not all(isinstance(k, str) and isinstance(v, t) for k, v in node.items()):
                raise PolicyError(f"{where}: 문자열 → {t.__name__} 맵이어야 한다")
    elif schema is float:
        if not isinstance(node, (int, float)) or isinstance(node, bool):
            raise PolicyError(f"{where}: 숫자여야 한다")
    elif schema is bool:
        if not isinstance(node, bool):
            raise PolicyError(f"{where}: bool이어야 한다")
    elif not isinstance(node, schema) or isinstance(node, bool):
        raise PolicyError(f"{where}: {schema.__name__}이어야 한다")


def _compile(pattern: str, where: str):
    try:
        return re.compile(pattern)
    except re.error as e:
        raise PolicyError(f"{where}: 정규식 오류 {e}") from e


@dataclass(frozen=True)
class Policy:
    ref: str
    hash: str
    raw: dict
    path: str

    def __getitem__(self, k):
        return self.raw[k]


def load(path: Path | str = DEFAULT_PATH) -> Policy:
    p = Path(path).resolve()
    if RUNS.resolve() in p.parents:
        raise PolicyError("정책 파일이 SEL이 쓰는 경로(runs/) 안에 있다")
    data = p.read_bytes()
    try:
        d = yaml.load(data.decode("utf-8"), Loader=_UniqueKeyLoader)
    except PolicyError:
        raise
    except Exception as e:
        raise PolicyError(f"YAML 파싱 실패: {e}") from e
    if not isinstance(d, dict) or d.get("schema_version") not in SCHEMA_VERSIONS:
        raise PolicyError(f"모르는 schema_version: {d.get('schema_version') if isinstance(d, dict) else None!r}")
    _validate(d, SCHEMA, "")
    if d["default"] != "deny":
        raise PolicyError("default는 deny만 허용")
    if d["checks"] != CHECKS:
        raise PolicyError(f"checks는 {CHECKS} 순서 그대로여야 한다")
    if not re.fullmatch(r"sel-governance-policy@\d+\.\d+\.\d+", d["ref"]):
        raise PolicyError(f"ref 형식: {d['ref']}")
    if not set(d["change_kinds"].values()) <= ASSET_KINDS:
        raise PolicyError("change_kinds 값은 자산 종류여야 한다")
    for path, (lo, hi) in RANGES.items():
        v = d[path[0]][path[1]]
        if not lo <= v <= hi:
            raise PolicyError(f"{'.'.join(path)}: {v}는 {lo}~{hi} 밖")
    for path in NONEMPTY:
        v = d
        for k in path:
            v = v[k]
        if not v or (isinstance(v, list) and not all(str(x).strip() for x in v)):
            raise PolicyError(f"{'.'.join(path)}: 비어 있으면 안 된다")
    for x in d["leak"]["identifiers"]:
        if set(x) - IDENT_KEYS or not {"id", "name", "pattern"} <= set(x):
            raise PolicyError(f"leak.identifiers 항목 키: {sorted(x)}")
        _compile(x["pattern"], "leak.identifiers")
    for x in d["synthetic"]["reserved_numbers"]:
        if set(x) != RESERVED_KEYS:
            raise PolicyError(f"synthetic.reserved_numbers 항목 키: {sorted(x)}")
        _compile(x["pattern"], "synthetic.reserved_numbers")
    if d["grade"]["most_sensitive"] not in ("lowest", "highest") or d["grade"]["unknown"] != "reject":
        raise PolicyError("grade 설정 값")
    return Policy(d["ref"], hashlib.sha256(data).hexdigest(), d, str(p))


@functools.lru_cache(maxsize=4)
def _cached(path: str) -> Policy:
    return load(path)


def default() -> Policy:
    return _cached(str(DEFAULT_PATH))


# ---------- 허용 주소 ----------

def _field_match(pattern: str, field: tuple) -> bool:
    parts = pattern.split(".")
    return len(parts) == len(field) and all(p == "*" or p == f for p, f in zip(parts, field))


def _norm_title(s: str) -> str:
    return " ".join(s.split()).lower()


def is_example_section(pol: Policy, section: tuple) -> bool:
    ex = {_norm_title(x) for x in pol["allow"]["skill"]["example_sections"]}
    return any(_norm_title(s) in ex for s in section)          # 예시 섹션의 하위도 예시


def allowed(asset, addr, pol: Policy | None = None) -> bool:
    """기본 거부. 정책 allow에 맞는 주소만 True."""
    pol = pol or default()
    a = pol["allow"]
    if asset.kind == "tool" and addr.mode == "field":
        return any(_field_match(p, addr.field) for p in a["tool"]["fields"])
    if asset.kind == "agent" and addr.mode == "field":
        return any(addr.field == ("spec", "params", p) for p in a["agent"]["params"])
    if asset.kind == "skill" and addr.mode == "section":
        deny = {_norm_title(x) for x in a["skill"]["deny_sections"]}
        return not any(_norm_title(s) in deny for s in addr.section)
    if asset.kind == "prompt" and addr.mode == "section":
        return asset.meta.get("role") not in a["prompt"]["deny_roles"]
    return False


def allowed_fn(pol: Policy | None = None):
    pol = pol or default()
    return lambda asset, addr: allowed(asset, addr, pol)
