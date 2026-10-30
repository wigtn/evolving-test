"""① finding 스키마(① 담당 sel-ingest finding.v1 + ② 요청 확장 2개)를 sel-lab finding이 지키는가."""
import json
import re

import pytest

from sel_lab.config import DATA

SCHEMA = json.loads((DATA / "contracts" / "finding.v1.schema.json").read_text(encoding="utf-8"))
SYNCUP_PATTERNS = set(SCHEMA["properties"]["pattern"]["enum"])


def validate(obj, schema, path="$"):
    """이 스키마가 쓰는 만큼만 검사한다(required · enum · const · pattern · type · additionalProperties · 길이 · 범위).
    jsonschema를 의존성에 넣지 않으려고 둔 최소 검사기."""
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    py = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "null": type(None)}
    if types and not any(isinstance(obj, py[x]) and not (x in ("integer", "number") and isinstance(obj, bool)) for x in types):
        raise AssertionError(f"{path}: type {types}")
    if "enum" in schema and obj not in schema["enum"]:
        raise AssertionError(f"{path}: {obj!r} not in enum")
    if "const" in schema and obj != schema["const"]:
        raise AssertionError(f"{path}: const")
    if isinstance(obj, str) and "pattern" in schema and not re.search(schema["pattern"], obj):
        raise AssertionError(f"{path}: pattern {schema['pattern']}")
    if isinstance(obj, (int, float)):
        assert schema.get("minimum", obj) <= obj <= schema.get("maximum", obj), f"{path}: range"
    if isinstance(obj, list):
        assert schema.get("minItems", 0) <= len(obj) <= schema.get("maxItems", len(obj)), f"{path}: items"
        for i, x in enumerate(obj):
            validate(x, schema.get("items", {}), f"{path}[{i}]")
    if isinstance(obj, dict):
        for k in schema.get("required", []):
            assert k in obj, f"{path}: missing {k}"
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(obj) - set(props)
            assert not extra, f"{path}: extra {sorted(extra)}"
        for k, v in obj.items():
            if k in props:
                validate(v, props[k], f"{path}.{k}")


def _fixtures():
    rows = [json.loads(x) for x in (DATA / "fixtures" / "findings.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r["pattern"] in SYNCUP_PATTERNS]      # eval_fail · no_asset_match는 lab 전용


@pytest.mark.parametrize("f", _fixtures(), ids=lambda f: f["finding_id"])
def test_fixture_finding_matches_contract(f):
    validate({k: v for k, v in f.items() if k != "_lab"}, SCHEMA)


def test_validator_rejects_extra_field():
    f = dict(_fixtures()[0], distinct_principals=3)
    with pytest.raises(AssertionError):
        validate({k: v for k, v in f.items() if k != "_lab"}, SCHEMA)


def test_dedupe_key_has_no_version():
    for f in _fixtures():
        assert "@" not in f["dedupe_key"], f["dedupe_key"]


def test_lab_only_patterns_are_marked():
    rows = [json.loads(x) for x in (DATA / "fixtures" / "findings.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert {r["pattern"] for r in rows} - SYNCUP_PATTERNS <= {"eval_fail", "no_asset_match"}
