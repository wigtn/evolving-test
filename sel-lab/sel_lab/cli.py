"""sel-lab CLI.

  python -m sel_lab.cli init                      # 활성 버전을 정상 자산으로 초기화
  python -m sel_lab.cli inject D01                # 결함 버전을 활성으로 (운영에 결함이 들어간 상황)
  python -m sel_lab.cli traffic --batch b1 --n 120
  python -m sel_lab.cli analyze --batch b1 --baseline b0
  python -m sel_lab.cli cycle                     # 열린 케이스를 승인 대기까지 진행
  python -m sel_lab.cli cases | show CASE | approve CASE | reject CASE --reason ... | verify CASE --batch b2 | rollback CASE
  python -m sel_lab.cli truth                     # 정답지와 비교 (결함을 잡고 고쳤나)
  python -m sel_lab.cli demo D01                  # 위 전체를 결함 하나로 끝까지
"""
from __future__ import annotations

import argparse
import json
import random
import shutil

import yaml

from . import analyzer, registry, scorer, sel, simulator, tester, tracing, worker
from .config import DATA, RUNS, offline


def _catalog():
    return yaml.safe_load((DATA / "defects" / "catalog.yaml").read_text(encoding="utf-8"))


def cmd_init(a):
    if a.clean and RUNS.exists():
        shutil.rmtree(RUNS)
    st = registry.init_state(force=True)
    print("active:", json.dumps(st["active"], ensure_ascii=False, indent=1))
    print("mode:", "OFFLINE (규칙 기반 가짜 에이전트 + oracle Improver)" if offline() else "LLM (사내 Gateway)")


def _defect(did):
    d = next((x for x in _catalog()["defects"] if x["id"] == did), None)
    if not d:
        raise SystemExit(f"알 수 없는 결함 ID: {did!r}. data/defects/catalog.yaml의 D01–D10 중 하나")
    return d


def cmd_inject(a):
    d = _defect(a.defect)
    if d["patch"]["op"] == "fault":
        st = registry.read_state()
        st["faults"][d["target"].split("@")[0]] = {d["patch"]["kind"]: d["patch"]["rate"]}
        registry.write_state(st)
        print(f"{a.defect}: 장애 주입 {d['target']} {d['patch']}")
    elif d.get("degraded_version"):
        ref = f"{d['target'].split('@')[0]}@{d['degraded_version']}"
        registry.set_active(ref, f"inject {a.defect}", "lab")
        print(f"{a.defect}: 활성 → {ref}")
    else:
        print(f"{a.defect}: 자산 변경 없음 ({d['patch']['op']}). traffic이 신호를 만든다")


def traffic(batch: str, n: int, seed: int = 0, recon: int = 0, focus: dict | None = None, log=print):
    """운영 트래픽 시뮬레이션: dev 풀의 요청을 운영 계정으로 실행하고, 결과에 따라 세션 이벤트를 남긴다.
    focus: {kind: 가중치}. 업무 구성 비율을 바꾼다 (기본은 사례 수 비례)."""
    rng = random.Random(seed)
    dev = [c for c in tester.cases().values() if c["pool"] == "dev"]
    weights = [(focus or {}).get(c["kind"], 1.0) for c in dev]
    users = simulator.personas()["users"]
    run_errors = 0
    for i in range(n):
        c = rng.choices(dev, weights=weights)[0]
        typical = [u["id"] for u in users if any(c["kind"].startswith(t) for t in u.get("typical", []))]
        uid = rng.choice(typical or [u["id"] for u in users])
        # 운영 기록에는 평가 케이스 ID를 넣지 않는다. 시뮬레이터 대응표는 sim_index에 따로 둔다 (루프는 읽지 않는다)
        r = worker.run(c["input"]["user_message"], user_id=uid, source="prod", simulator=simulator.CaseSimulator(c),
                       seed=rng.randint(0, 1 << 16), batch=batch)
        if r["status"] == "runtime_error":
            run_errors += 1
            if i >= 9 and run_errors / (i + 1) > 0.3:   # 연결·인증 문제를 "결함을 못 잡았다"로 오인하지 않게 중단
                raise SystemExit(f"실행 실패율 {run_errors}/{i + 1}. 연결·키·모델을 확인하라 (sel smoke). 마지막 오류: {r['followups'][-1:]}")
        s = scorer.score(c, r)
        tracing._append("sim_index", {"run_id": r["run_id"], "case_id": c["eval_case_id"], "batch": batch})
        tracing.record_session({"batch": batch, "session_id": f"s-{r['run_id']}", "run_id": r["run_id"], "user_id": uid,
                                "asset_refs": r["asset_refs"],
                                "events": simulator.session_events(c, s, uid, rng) if s["score"] is not None else []})
        if (i + 1) % 20 == 0:
            log(f"  traffic {batch}: {i + 1}/{n}")
    recon_users = [u for u in users if u.get("repetitive_procedure")]
    normal = [v["name1"] for v in worker.world().vendors if v["profile"] == "normal"]
    for j in range(recon):
        u = recon_users[j % len(recon_users)]
        msg = simulator.recon_request(u["id"], rng.sample(normal, 3))
        r = worker.run(msg, user_id=u["id"], source="prod", seed=j, batch=batch)
        tracing.record_session({"batch": batch, "session_id": f"s-{r['run_id']}", "run_id": r["run_id"], "user_id": u["id"],
                                "asset_refs": r["asset_refs"], "events": [],
                                "procedure": u["repetitive_procedure"], "covered_by_skill": None})


def golden(batch: str, k: int = 1):
    """정기 골든셋: 고정 세트(SME 필수 사례, dev 풀)를 활성 버전으로 실행한다 (R6 입력)."""
    fixed = [c for c in tester.cases().values() if c["fixed"] and c["pool"] == "dev"]
    agent_ref = registry.active_ref(registry.DEFAULT_AGENT)
    for c in fixed:
        for rep in range(k):
            row = tester.run_case(c, None, arm="golden", repeat=rep)
            tracing._append("golden", {"batch": batch, "case_id": c["eval_case_id"], "run_id": row["run_id"],
                                       "agent_ref": agent_ref, "passed": bool(row["passed"]), "checks": row["checks"]})


def _focus(s):
    return {k: float(v) for k, v in (x.split(":") for x in s.split(","))} if s else None


def preflight():
    """LLM 모드 시작 전 1회: 모델 호출과 tool calling이 되는지. 실패하면 바로 중단한다."""
    r = worker.run("가상거래처-0007 9월 미결 알려줘", user_id="U9001", source="eval", seed=0)
    called = [c["name"] for c in r["tool_calls"]]
    if r["status"] == "runtime_error" or not called:
        raise SystemExit(f"preflight 실패: status={r['status']} tool_calls={called} {r['followups'][-1:]}\n"
                         "sel smoke로 연결·tool calling을 확인하라")
    print(f"[preflight] OK tool_calls={called}")


def cmd_traffic(a):
    if not offline():
        preflight()
    traffic(a.batch, a.n, a.seed, a.recon, _focus(a.focus))
    golden(a.batch)
    print(f"traffic {a.batch} 완료 (+ 정기 골든셋)")


def _print_analysis(res):
    for f in res["findings"]:
        m = f["metrics"]
        print(f"  FINDING {f['signal']:<18} {str(f['target']['asset_ref'] or f['target'].get('procedure')):<45} "
              f"rate={m['rate']} base={m['baseline_rate']} support={m['support']} users={m['distinct_principals']}")
    for f in res["below_threshold"]:
        m = f["metrics"]
        print(f"  (임계 미달) {f['signal']:<14} {str(f['target']['asset_ref']):<45} rate={m['rate']} support={m['support']}")
    for p in res["platform_signals"]:
        print(f"  PLATFORM {p}")


def cmd_analyze(a):
    res = analyzer.analyze(a.batch, a.baseline)
    _print_analysis(res)
    if not a.dry:
        print("opened:", sel.ingest(res["findings"]))


def cmd_cycle(a):
    for c in sel.list_cases():
        if c["state"] not in sel.TERMINAL | {"AWAITING_APPROVAL", "VERIFYING", "HELD"}:
            print(f"{c['case_id']} → {sel.run_until_wait(c['case_id'], k=a.k)}")


def cmd_cases(a):
    for c in sel.list_cases():
        print(f"{c['case_id']}  {c['state']:<18} iter={c['iteration']} {c['signal']:<18} {c['target_asset']}")


def cmd_show(a):
    case, ev = sel.show(a.case)
    b = case["body"]
    print(json.dumps({"state": case["state"], "iteration": case["iteration"], "hypothesis": b.get("hypothesis"),
                      "candidate": b.get("candidate"), "gate": b.get("gate"), "verification": b.get("verification")},
                     ensure_ascii=False, indent=1))
    for e in ev:
        print(f"  {e['at']} {e['from_state']} → {e['to_state']} ({e['actor']}) {e['payload'][:160]}")
    if a.diff and b.get("candidate"):
        import difflib
        h = b["hypothesis"]
        old, new = registry.load(h["target_asset_ref"]).text, registry.load(b["candidate"]["ref"]).text
        print("\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), h["target_asset_ref"], b["candidate"]["ref"], lineterm="")))


def cmd_approve(a):
    print(sel.approve(a.case, a.approver, a.comment))


def cmd_reject(a):
    print(sel.reject(a.case, a.approver, a.reason))


def cmd_verify(a):
    res = analyzer.analyze(a.batch, None)
    print(sel.verify(a.case, res))


def cmd_rollback(a):
    print(sel.rollback(a.case, a.approver))


def cmd_truth(a):
    """정답지(ground_truth)와 비교. 루프에는 넣지 않고 채점에만 쓴다."""
    gt = {d["id"]: d for d in _catalog()["defects"]}
    st = registry.read_state()
    for c in sel.list_cases():
        case, _ = sel.show(c["case_id"])
        h = case["body"].get("hypothesis") or {}
        print(f"{c['case_id']} {c['state']:<12} signal={c['signal']} target={h.get('target_asset_ref')} change={h.get('change_type')}")
    print("active:", {k: v for k, v in st["active"].items()})
    injected = [h for h in st["history"] if h["reason"].startswith("inject ")]
    for h in injected:
        did = h["reason"].split()[1]
        d = gt[did]
        now = st["active"].get(h["base"])
        if now == h["to"]:
            print(f"  {did}: 결함 버전이 아직 활성 ({h['base']}@{now})")
        else:
            print(f"  {did}: 결함 버전 {h['to']} → {now} 로 교체됨. 정답 수정: {d['expect'].get('fix')}")


def cmd_smoke(a):
    """키를 넣은 뒤 첫 실행 전 확인: 연결, tool calling, JSON 출력."""
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import StructuredTool

    from .config import chat_model, env
    if offline():
        raise SystemExit("오프라인 모드다. .env에 SEL_LLM_BASE_URL, SEL_LLM_API_KEY, SEL_MODEL을 넣고 SEL_OFFLINE=0으로 둔다")
    print("base_url:", env("SEL_LLM_BASE_URL"))
    ok = True
    for role in ("agent", "creator", "improver"):
        m = chat_model(role)
        print(f"[{role}] model={m.model_name}")
        try:
            r = m.invoke([HumanMessage("ping이라고만 답해")])
            print("  연결: OK", repr(r.content)[:60])
        except Exception as e:
            print("  연결: 실패", repr(e)[:300])
            ok = False
            continue
        if role == "agent":
            t = StructuredTool.from_function(func=lambda name_query: "{}", name="fi_vendor_search", description="거래처 검색",
                                             args_schema={"type": "object", "properties": {"name_query": {"type": "string"}}, "required": ["name_query"]})
            r = m.bind_tools([t]).invoke([HumanMessage("가상거래처-0007을 검색해줘. 도구를 써라")])
            tc = getattr(r, "tool_calls", None)
            print("  tool calling:", "OK " + json.dumps(tc[0]["args"], ensure_ascii=False) if tc else "실패 (도구를 부르지 않음)")
            ok &= bool(tc)
        else:
            from .sel import _json_from
            r = m.invoke([HumanMessage('{"decision":"proceed","confidence":0.5} 형식의 JSON 하나만 답해')])
            try:
                print("  JSON:", "OK", _json_from(r.content))
            except Exception as e:
                print("  JSON: 실패", repr(e)[:200])
                ok = False
    print("smoke:", "통과" if ok else "실패")


def cmd_aa(a):
    """A/A: 같은 버전끼리 게이트를 돌려 오판률을 잰다 (PRD v0.5 6장, M2c 착수 조건).
    오프라인 가짜 에이전트는 결정적이라 오판이 0으로 나온다. 의미 있는 값은 사내 LLM으로 잰다."""
    from . import gate
    from .sel import build_eval_set
    if not offline():
        preflight()
    registry.init_state(force=False)
    kinds = sorted({c["kind"] for c in tester.cases().values()})
    trials = {"G1_pass": 0, "G2_fail": 0, "G3a_fail": 0, "G3b_fail": 0, "regress>allow": 0, "verdict_pass": 0}
    for i in range(a.trials):
        h = {"target_kinds": [kinds[i % len(kinds)]], "guard_kinds": [kinds[(i + 1) % len(kinds)]]}
        es = build_eval_set(h)
        res = tester.paired_run(es, {}, k=a.k, log=lambda *_: None)   # 후보 = 기준과 같은 버전
        d = gate.decide(res)
        trials["G1_pass"] += d["G1"]
        trials["G2_fail"] += not d["G2"]
        trials["G3a_fail"] += not d["G3a"]
        trials["G3b_fail"] += not d["G3b"]
        trials["regress>allow"] += d["regressed_cases"] > d["regress_allow"]
        trials["verdict_pass"] += d["verdict"] == "PASS"
        print(f"  trial {i + 1}: target={h['target_kinds']} verdict={d['verdict']} G1={d['G1']} G2={d['G2']} "
              f"G3a={d['G3a']} G3b={d['G3b']} regressed={d['regressed_cases']}/{d['regress_allow']} d̄={d['target']['dbar']:.3f}")
    print("A/A 오판률 (목표: 각각 5% 이하, 판단에는 59회 이상 필요):",
          {k: f"{v}/{a.trials}" for k, v in trials.items()})


def cmd_demo(a):
    """결함 하나를 처음부터 끝까지: 기준 트래픽 → 주입 → 운영 트래픽 → 분석 → 루프 → 승인 → 반영 후 트래픽 → 검증."""
    log = print
    _defect(a.defect)
    if not offline():
        preflight()
    if RUNS.exists():
        shutil.rmtree(RUNS)
    registry.init_state(force=True)
    print(f"[mode] {'OFFLINE (규칙 기반 가짜 에이전트 + oracle Improver: 배관 검증용)' if offline() else 'LLM'}")
    print("[1] 기준 트래픽 b0 (정상 자산)")
    traffic("b0", a.n, seed=1, focus=_focus(a.focus), log=log)
    golden("b0")
    print(f"[2] 결함 주입 {a.defect}")
    cmd_inject(argparse.Namespace(defect=a.defect))
    print("[3] 운영 트래픽 b1")
    traffic("b1", a.n, seed=2, recon=a.recon, focus=_focus(a.focus), log=log)
    golden("b1")
    print("[4] 분석 (b1 vs 기준 b0)")
    res = analyzer.analyze("b1", "b0")
    _print_analysis(res)
    opened = sel.ingest(res["findings"])
    print("    opened:", opened)
    for cid in opened:
        print(f"[5] 루프 {cid}")
        st = sel.run_until_wait(cid, k=a.k, log=log)
        case, _ = sel.show(cid)
        g = case["body"].get("gate")
        if g:
            print(f"    gate: {g['verdict']} G1={g['G1']} G2={g['G2']} G3={g['G3']} G4={g['G4']} regressed={g['regressed_cases']} "
                  f"target d̄={g['target']['dbar']:.3f} lcb={g['target']['lcb']:.3f} mmi={g['mmi']} n={g['target']['n']}")
        print(f"    state: {st}")
        if st == "AWAITING_APPROVAL" and a.auto_approve:
            print("[6] 승인 (demo: 자동 승인. 실제로는 사람이 Admin UI에서)")
            sel.approve(cid, "demo-approver", "demo")
            print("[7] 반영 후 트래픽 b2 → 검증")
            traffic("b2", a.n, seed=3, focus=_focus(a.focus), log=log)
            golden("b2")
            print("    ", sel.verify(cid, analyzer.analyze("b2", None)), sel.show(cid)[0]["body"].get("verification"))
    print("[8] 정답지 대조")
    cmd_truth(a)


def main():
    p = argparse.ArgumentParser(prog="sel")
    sp = p.add_subparsers(required=True)
    x = sp.add_parser("init"); x.add_argument("--clean", action="store_true"); x.set_defaults(f=cmd_init)
    x = sp.add_parser("inject"); x.add_argument("defect"); x.set_defaults(f=cmd_inject)
    x = sp.add_parser("traffic"); x.add_argument("--batch", required=True); x.add_argument("--n", type=int, default=120)
    x.add_argument("--seed", type=int, default=0); x.add_argument("--recon", type=int, default=0)
    x.add_argument("--focus", help="업무 구성 가중치. 예: open_items_ambiguous:5,document_post:3"); x.set_defaults(f=cmd_traffic)
    x = sp.add_parser("analyze"); x.add_argument("--batch", required=True); x.add_argument("--baseline")
    x.add_argument("--dry", action="store_true"); x.set_defaults(f=cmd_analyze)
    x = sp.add_parser("cycle"); x.add_argument("--k", type=int, default=3); x.set_defaults(f=cmd_cycle)
    x = sp.add_parser("cases"); x.set_defaults(f=cmd_cases)
    x = sp.add_parser("show"); x.add_argument("case"); x.add_argument("--diff", action="store_true"); x.set_defaults(f=cmd_show)
    x = sp.add_parser("approve"); x.add_argument("case"); x.add_argument("--approver", default="me"); x.add_argument("--comment", default="")
    x.set_defaults(f=cmd_approve)
    x = sp.add_parser("reject"); x.add_argument("case"); x.add_argument("--approver", default="me"); x.add_argument("--reason", required=True)
    x.set_defaults(f=cmd_reject)
    x = sp.add_parser("verify"); x.add_argument("case"); x.add_argument("--batch", required=True); x.set_defaults(f=cmd_verify)
    x = sp.add_parser("rollback"); x.add_argument("case"); x.add_argument("--approver", default="me"); x.set_defaults(f=cmd_rollback)
    x = sp.add_parser("truth"); x.set_defaults(f=cmd_truth)
    x = sp.add_parser("smoke"); x.set_defaults(f=cmd_smoke)
    x = sp.add_parser("aa"); x.add_argument("--trials", type=int, default=59); x.add_argument("--k", type=int, default=3)
    x.set_defaults(f=cmd_aa)
    x = sp.add_parser("demo"); x.add_argument("defect"); x.add_argument("--n", type=int, default=120)
    x.add_argument("--k", type=int, default=3); x.add_argument("--recon", type=int, default=0)
    x.add_argument("--auto-approve", action="store_true"); x.add_argument("--focus"); x.set_defaults(f=cmd_demo)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
