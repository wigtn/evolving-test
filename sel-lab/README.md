# sel-lab

SEL(self-evolving loop)을 로컬에서 처음부터 끝까지 돌려보는 실험장. LangGraph + DeepAgents 워커, 가짜 SAP 세계, 결함 주입 자산 위에서
**finding → Creator → Improver → Tester(쌍대) → Evaluator → Gate → 승인 → 릴리스 → Verifier**를 실행한다.
설계 기준은 SEL 애플리케이션 설계 v0.1(케이스 상태머신, finding 계약, R1–R8, 게이트 G1–G4).

docker가 필요 없다. Python 3.11 이상과 패키지 설치만 되면 된다.

## 설치

```bash
cd sel-lab
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt && .venv/bin/pip install -e . --no-deps   # 검증한 버전 그대로
# Windows: .venv\Scripts\pip ... , 출력 깨짐 방지로 set PYTHONUTF8=1
# 사내 PyPI 미러: pip install -i <미러 URL> ... , 사내 인증서가 필요하면 SSL_CERT_FILE 지정
```

`requirements.lock.txt`는 2026-10-27에 검증한 버전이다 (langgraph 1.2.18, langchain 1.4.7, langchain-openai 1.7.3, deepagents 0.7.32).

## LLM 키 넣기 (사내 OpenAI 호환 Gateway)

```bash
cp .env.example .env     # .env는 커밋되지 않는다
```

`.env`에서 채울 것:

| 변수 | 내용 |
| --- | --- |
| `SEL_LLM_BASE_URL` | OpenAI 호환 엔드포인트 (예: `https://<gateway>/v1`) |
| `SEL_LLM_API_KEY` | 키 |
| `SEL_MODEL` | 기본 모델명. 역할별로 `SEL_AGENT_MODEL`, `SEL_CREATOR_MODEL`, `SEL_IMPROVER_MODEL`, `SEL_JUDGE_MODEL`로 덮어쓸 수 있다 |
| `SEL_OFFLINE` | `1`이면 키가 있어도 오프라인 모드 |

DeepSeek 같은 OpenAI 호환 모델이면 그대로 동작한다. 확인할 것:
- **에이전트 모델은 tool calling을 지원해야 한다.** 첫 실행 전에 `python -m sel_lab.cli smoke`로 확인한다.
- **에이전트에는 non-thinking(채팅) 모델을 먼저 쓴다.** 추론(thinking) 모드와 tool calling을 함께 쓰면 provider별 제약이 있을 수 있다(확인 안 됨).
- Creator·Improver 출력은 provider 전용 structured output에 기대지 않고, 답에서 JSON을 직접 파싱한다.
- judge는 Improver와 다른 모델이나 다른 프롬프트로 쓰는 것이 원칙이다(앱 설계 6.4). 지금은 judge를 끄고(`SEL_JUDGE_ENABLED=0`) 규칙 채점만으로 게이트를 판정한다.

키가 없거나 `SEL_OFFLINE=1`이면 **오프라인 모드**로 돈다:
- 에이전트 = 규칙 기반 가짜 모델(`fake_llm.py`). 자산에 적힌 지시가 있으면 따르고, 없으면 틀린다. 그래서 결함 버전을 넣으면 실제로 실패한다.
- Creator = 증거 기반 규칙 대역 (정답지를 보지 않는다).
- Improver = **ORACLE**. 정상 버전 원문으로 되돌린다. 배관 검증 전용이며 개선 능력을 재지 않는다.

## 실행

**LLM 모드 첫 실행은 작게** 한다. demo(n=250)는 에이전트 실행이 약 850회라 실제 모델이면 수 시간이 걸린다.

```bash
python -m sel_lab.cli smoke                                   # 연결 · tool calling · JSON 확인
python -m sel_lab.cli demo D01 --n 40 --k 1 --auto-approve    # LLM 모드 첫 시도 (임계 미달이면 n을 늘린다)
```

LLM 모드에서는 시작할 때 preflight(요청 1건, 도구 호출 여부)를 하고, 트래픽 중 실행 실패율이 30%를 넘으면 중단한다. 연결 문제를 "결함을 못 잡았다"로 오인하지 않기 위해서다.

오프라인 검증:

```bash
# 결함 하나로 처음부터 끝까지 (기준 트래픽 → 결함 주입 → 운영 트래픽 → 분석 → 루프 → 승인 → 반영 후 트래픽 → 검증)
python -m sel_lab.cli demo D01 --n 250 --k 3 --auto-approve
python -m sel_lab.cli demo D02 --n 250 --auto-approve --focus open_items_ambiguous:8   # 드문 업무는 비중을 올려야 임계를 넘는다
```

단계별로 돌리기:

```bash
python -m sel_lab.cli init --clean                      # 활성 버전 = 정상 자산
python -m sel_lab.cli traffic --batch b0 --n 250        # 기준선 운영 트래픽 (+ 정기 골든셋)
python -m sel_lab.cli inject D04                        # 결함 버전을 활성으로
python -m sel_lab.cli traffic --batch b1 --n 250
python -m sel_lab.cli analyze --batch b1 --baseline b0  # finding → 케이스 개설
python -m sel_lab.cli cycle                             # 케이스를 승인 대기까지
python -m sel_lab.cli cases
python -m sel_lab.cli show <case_id> --diff             # 가설, 게이트 통계, 후보 diff, 상태 이력
python -m sel_lab.cli approve <case_id>                 # 사람 승인 → 활성 전환
python -m sel_lab.cli traffic --batch b2 --n 250
python -m sel_lab.cli verify <case_id> --batch b2       # 반영 후 같은 패턴 발생률 비교
python -m sel_lab.cli truth                             # 정답지와 대조 (채점용. 루프에는 쓰지 않는다)
```

결과물은 `runs/`에 쌓인다: `traces/runs_prod.jsonl`, `runs_eval.jsonl`, `audit.jsonl`(Gateway 감사 기록), `sessions.jsonl`(세션 기록), `golden.jsonl`, `sel.sqlite`(케이스·이벤트), `registry/`(활성 버전, 후보 자산).

trace 화면이 필요하면 `pip install -e ".[phoenix]"` 후 `phoenix serve`를 띄우고 `.env`에 `SEL_OTLP_ENDPOINT=http://localhost:6006/v1/traces`를 넣는다.

## 오프라인 검증 결과 (n=250, k=3)

| 결함 | 신호 | 루프 결과 |
| --- | --- | --- |
| D01 기간 기본값 삭제 | R1 tool_error (ROW_LIMIT) | Creator가 대상을 도구가 아니라 SKILL로 짚음 → 게이트 통과 → VERIFIED (오류율 11–13% → 2%) |
| D02 되묻기 삭제 | R5 재요청 | `--focus`로 비중을 올리면 VERIFIED. 기본 구성에서는 드물어서 임계(지지 20, 주체 3) 미달 |
| D03 금액 표기 삭제 | R5 수정 | VERIFIED |
| D04 승인 단계 삭제 | R6 정기 골든셋 실패 | VERIFIED. 운영 트래픽만으로는 전기 담당이 한 명이라 주체 수 임계에 걸림 |
| D05 max_turns 4 | R8 budget_exceeded | VERIFIED |
| D06 도구 설명에서 형식 삭제 | R2 arg_invalid | VERIFIED. 같은 원인의 재요청 케이스는 STALE 규칙으로 닫힘 |
| D07 도구 장애 | 플랫폼 신호 | 자산 케이스를 열지 않음 |

이 결과는 **배관이 맞게 연결됐다**는 뜻이다. 오프라인 Improver가 정답(정상 버전)을 내므로, 개선 능력은 사내 LLM으로 돌려야 잴 수 있다.

## 구성

| 파일 | 역할 |
| --- | --- |
| `sel_lab/world.py` | 가짜 SAP 세계 + Tools Gateway (row limit, 인자 검증, dry_run, 승인 토큰, 평가 계정 쓰기 차단, 장애 주입, 감사 기록) |
| `sel_lab/registry.py` | 자산 버전과 활성 포인터, `eval.asset_overrides`, 후보 등록 |
| `sel_lab/worker.py` | 워커. 자산 묶음으로 에이전트를 조립. 기본은 LangChain `create_agent`, `SEL_WORKER=deepagents`면 DeepAgents. `max_turns`는 ModelCallLimitMiddleware. 잘못된 도구 인자는 ARG_INVALID로 감사 기록에 남긴다 |
| `sel_lab/scorer.py` | 규칙 채점 |
| `sel_lab/tester.py` | 쌍대 실행 (기준 팔 · 후보 팔 × k) |
| `sel_lab/gate.py` | G1–G4 (LCB, MDI, α' = 0.05/3) |
| `sel_lab/analyzer.py` | 폴백 분석기 R1, R2, R5, R6, R7, R8 |
| `sel_lab/sel.py` | 케이스 상태머신, Creator, Improver, 정적 검사, 승인·릴리스·검증·롤백 |
| `sel_lab/cli.py` | 명령 |
| `gen/build.py` | 가짜 데이터 재생성 |

## 워커를 create_agent로 둔 이유

DeepAgents `create_deep_agent`는 파일 도구(ls, read_file, write_file 등)와 `task` 서브에이전트를 기본으로 붙인다. 서브에이전트가 부른 도구는 메인 대화 기록에 남지 않아 채점(승인 순서, 되묻기, 도구 인자)이 조용히 틀린다. 그래서 기본 워커는 우리가 준 도구만 노출하는 `create_agent`(LangGraph 기반)다. 운영 설계가 DeepAgents라면 `SEL_WORKER=deepagents`로 바꾸되, 서브에이전트 기록을 수집하도록 고쳐야 한다.

## 정답 누수 방지

- 결함 버전은 평범한 버전 번호를 쓴다 (`-d01` 같은 표지 없음). 규칙 마커는 원본(`data/assets_src/`)에만 있고 루프가 보는 `data/assets/`에는 없다.
- 운영 실행 기록에는 평가 케이스 ID를 넣지 않는다. 시뮬레이터 대응표는 `runs/traces/sim_index.jsonl`에 따로 두고 루프는 읽지 않는다.
- 세션 기록에는 채점 항목 이름이 아니라 사용자 발화만 남긴다.
- Creator 가설의 대상은 항상 현재 활성 버전으로 강제한다. 판정용 holdout 사례 내용은 Creator·Improver에 넘기지 않는다.

## 아직 없는 것

- R3 retry_loop, R4 hitl_rejected
- judge 채점과 보정(7.4). 지금은 규칙 채점만
- new_asset(D09)의 후보 SKILL 생성과 카나리. 오프라인 모드는 new_asset을 DISMISS한다
- 카나리 릴리스. 지금은 full 충실도로 보고 전체 릴리스
- 판정용 held-out을 사람이 관리하는 화면. 지금은 생성기가 dev/holdout을 나눈다

## 데이터

`data/` 구성은 `gen/build.py` 머리말과 `data/defects/catalog.yaml`을 본다. `data/ground_truth/`는 채점용 정답이라 루프에 넣지 않는다.
