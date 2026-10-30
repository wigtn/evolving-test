"""W9 · 시험 문제 선정 (docs/10 W9, docs/11 2장)."""
from __future__ import annotations

import sqlite3

import pytest

from sel_lab import sel, tester, testset
from sel_lab.testset import classify, finding_signature, matches, probe, scope_kinds, select, stratify

TOOL = "asset://tool/fi/open-items-query"
SIG = {"outcome_type": "arg_error", "tool": TOOL, "result_code": "ROW_LIMIT_EXCEEDED"}


def run(passed, errors=(), status="ok", checks=None):
    return {"passed": passed, "errors": [{"tool": t, "error": e} for t, e in errors], "status": status,
            "checks": checks or {}}


ROW = run(False, [(TOOL, "ROW_LIMIT_EXCEEDED")])
OTHER = run(False, [], checks={"correct": False})


# ---------- 서명 ----------

def test_finding_signature_per_signal():
    ev = [{"error": "ROW_LIMIT_EXCEEDED"}, {"error": "ROW_LIMIT_EXCEEDED"}, {"error": "VENDOR_NOT_FOUND"}]
    assert finding_signature("tool_arg_error", TOOL + "@1.0.0", ev) == SIG
    assert finding_signature("tool_arg_error", TOOL + "@1.0.0", [{"error": "ARG_INVALID"}])["result_code"] == "ARG_INVALID"
    assert finding_signature("limit_exceeded", "asset://agent/fi/assistant@1.0.1", []) == {"outcome_type": "limit_exceeded"}
    assert finding_signature("eval_fail", "x", [{"failed_checks": ["policy"]}])["failed_checks"] == ["policy"]
    assert finding_signature("repeat_question", "asset://agent/fi/assistant@1.0.0", []) is None


def test_matches():
    assert matches(SIG, ROW) and not matches(SIG, OTHER) and not matches(SIG, run(True))
    assert not matches(SIG, run(False, [(TOOL, "VENDOR_NOT_FOUND")]))
    assert matches({"outcome_type": "limit_exceeded"}, run(False, status="budget_exceeded"))
    assert matches({"outcome_type": "eval_fail", "failed_checks": ["policy"]}, run(False, checks={"policy": False}))
    assert matches(None, OTHER)                                    # 서명 없음: 실패면 일치


# ---------- 분류 · 탐침 ----------

@pytest.mark.parametrize("runs,sig,expect", [
    ([run(True), run(True)], SIG, "normal"),
    ([ROW, ROW], SIG, "past_failure"),
    ([ROW, run(True), ROW], SIG, "past_failure"),                 # 경계 → 3회째 실패로 2/3
    ([ROW, run(True), run(True)], SIG, "excluded"),               # 경계 → 3회째 통과
    ([OTHER, OTHER], SIG, "repeat_failure"),                      # 서명 다른 반복 실패: 대체 후보
    ([OTHER, OTHER], None, "past_failure"),                       # 서명 없음: 실패 2회 이상
    ([run(None), run(True)], SIG, "excluded"),                    # 실행 실패는 판단 근거가 아님
])
def test_classify(runs, sig, expect):
    assert classify(runs, sig) == expect


def test_probe_runs_third_only_on_boundary():
    calls = []

    def runner(results):
        def f(case, ov, arm, repeat):
            calls.append(repeat)
            return results[repeat]
        return f
    case = next(iter(tester.cases().values()))
    calls.clear(); probe(case, SIG, runner([run(True), run(True)]))
    assert calls == [0, 1]
    calls.clear(); probe(case, SIG, runner([ROW, ROW]))
    assert calls == [0, 1]
    calls.clear(); c, _ = probe(case, SIG, runner([ROW, run(True), ROW]))
    assert calls == [0, 1, 2] and c == "past_failure"


# ---------- 범위 · 층화 ----------

def test_scope_kinds():
    k = sel.KINDS
    assert scope_kinds("asset://skill/fi/open-items@1.4.3", k) == {x for x, (b, _) in k.items() if b == "asset://skill/fi/open-items"}
    assert scope_kinds("asset://tool/fi/gl-balance-query@1.2.1", k) == {"gl_balance"}
    assert scope_kinds("asset://agent/fi/assistant@1.0.1", k) == set(k)
    assert scope_kinds("asset://prompt/fi/assistant-persona@1.0.1", k) == set(k)
    assert scope_kinds(None, k) == set(k)


def test_stratify_round_robin():
    cs = tester.cases()
    ids = [cid for cid, c in cs.items() if not c["fixed"]]
    out = stratify(ids, cs, 7, "s")
    kinds = [cs[x]["kind"] for x in out]
    assert len(out) == 7 and len(set(kinds)) == 7                 # 업무 유형마다 하나씩 먼저


# ---------- 선정 ----------

def _held():
    return {cid for cid, c in tester.cases().items() if not c["fixed"]}


def fake_runner(fail_kinds=(), sig_kinds=(), budget_kinds=()):
    def f(case, ov, arm, repeat):
        k = case["kind"]
        if k in sig_kinds:
            return ROW
        if k in budget_kinds:
            return run(False, status="budget_exceeded")
        if k in fail_kinds:
            return OTHER
        return run(True)
    return f


def test_select_prefers_signature_matches():
    es = select(_held(), target_ref="asset://skill/fi/open-items@1.4.3", finding={"pattern": "tool_arg_error", "signature": SIG},
                kinds_map=sel.KINDS, usage={}, seed="t", runner=fake_runner(sig_kinds={"open_items_no_period"},
                                                                         fail_kinds={"open_items_annual"}), log=lambda *_: None)
    cs = tester.cases()
    assert es["target"] and all(cs[x]["kind"] == "open_items_no_period" for x in es["target"])
    assert es["_stats"]["rule"] == "signature"


def test_select_falls_back_to_repeat_failures_and_records_it():
    es = select(_held(), target_ref="asset://skill/fi/open-items@1.4.3", finding={"pattern": "tool_arg_error", "signature": SIG},
                kinds_map=sel.KINDS, usage={}, seed="t", runner=fake_runner(fail_kinds={"open_items_no_period"}),
                log=lambda *_: None)
    assert es["target"] and es["_stats"]["rule"].startswith("repeat_failure(fallback")


def test_select_fills_guard_from_out_of_scope_when_scope_all_fails():
    es = select(_held(), target_ref="asset://tool/fi/gl-balance-query@1.2.1",
                finding={"pattern": "tool_arg_error", "signature": {"outcome_type": "arg_error", "tool": "asset://tool/fi/gl-balance-query", "result_code": "ARG_INVALID"}},
                kinds_map=sel.KINDS, usage={}, seed="t", runner=fake_runner(fail_kinds={"gl_balance"}), log=lambda *_: None)
    cs = tester.cases()
    assert len(es["guard"]) == 8 and all(cs[x]["kind"] != "gl_balance" for x in es["guard"])
    assert es["_stats"]["guard_out_of_scope"] == 8


def test_select_excludes_usage_capped_and_keeps_all_fixed():
    held = _held()
    capped = {cid: testset.USAGE_CAP for cid in held}
    es = select(held, target_ref=None, finding=None, kinds_map=sel.KINDS, usage=capped, seed="t",
                runner=fake_runner(), log=lambda *_: None)
    assert es["target"] == [] and es["guard"] == []
    assert set(es["fixed"]) == {cid for cid, c in tester.cases().items() if c["fixed"]}


def test_select_eval_fail_adds_failed_fixed_cases_to_target(monkeypatch):
    fx = sorted(cid for cid, c in tester.cases().items() if c["fixed"])[:2]
    from sel_lab import analyzer
    # 실패 사례는 finding에 실려 오지 않고 ②가 ①의 기록에서 읽는다 → 그 자리를 대신한다
    monkeypatch.setattr(analyzer, "failing_runs", lambda f: [{"case_id": x} for x in fx])
    es = select(_held(), target_ref="asset://skill/fi/document-post@1.0.1",
                finding={"pattern": "eval_fail", "signature": {"outcome_type": "eval_fail", "failed_checks": ["policy"]}},
                kinds_map=sel.KINDS, usage={}, seed="t", runner=fake_runner(), log=lambda *_: None)
    assert es["target"][:2] == fx and not set(fx) & set(es["fixed"])


# ---------- 고정 · 무효 · 사용 횟수 ----------

@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(sel, "DB", tmp_path / "sel.sqlite")
    return sel.db()


def _es():
    cs = sorted(tid for tid, c in tester.cases().items() if not c["fixed"])
    return {"target": cs[:2], "guard": cs[2:4], "fixed": []}


def test_freeze_is_deterministic_and_frozen(conn):
    a = testset.freeze(conn, "case_x", 1, _es())
    b = testset.freeze(conn, "case_x", 1, _es())
    assert a["id"] == b["id"] and testset.status(conn, a["id"]) == "frozen"


def test_exposure_after_freeze_invalidates_unjudged_set(conn):
    es = _es()
    ts = testset.freeze(conn, "case_x", 1, es)
    msg = tester.cases()[es["target"][0]]["input"]["user_message"]
    sel.expose("asset://agent/fi/assistant", [msg], "creator_evidence")
    assert testset.status(conn, ts["id"]) == "invalid"


def test_judged_set_is_not_invalidated_and_usage_counts(conn):
    es = _es()
    ts = testset.freeze(conn, "case_y", 1, es)
    testset.mark_judged(conn, ts["id"], es)
    sel.expose("asset://agent/fi/assistant", [tester.cases()[es["target"][0]]["input"]["user_message"]], "creator_evidence")
    assert testset.status(conn, ts["id"]) == "judged"
    use = testset.usage_map(conn)
    assert all(use[x] == 1 for x in es["target"] + es["guard"])


def test_exposed_inputs_never_enter_held_out(conn):
    cs = tester.cases()
    victim = sorted(cid for cid, c in cs.items() if not c["fixed"])[0]
    sel.expose("asset://agent/fi/assistant", [cs[victim]["input"]["user_message"]], "creator_evidence")
    msg = cs[victim]["input"]["user_message"]
    same = {cid for cid, c in cs.items() if c["input"]["user_message"] == msg}
    for seed in ("a", "b", "c"):
        assert not same & sel.split_golden(seed)
