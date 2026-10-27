# evolving-test

사내 ERP AI 플랫폼의 **SEL(self-evolving loop)** 설계 문서 모음.

SEL은 공용 자산(Agent, SKILL, Prompt)을 평가하고 고치는 루프다. **Agent Runtime VM(Aegra) 안**에서 스케줄러 신호로 Creator → Tester → Evaluator → Improver를 차례로 실행한다. 개선안은 자산 저장소에 등록·상신하고, 사람이 승인하면 활성 버전이 되어 다음 실행부터 모든 창구에 반영된다.

## 최종 아키텍처

![SEL 아키텍처](docs/images/sel-architecture-handdrawn.png)

| 단계 | 하는 일 |
| --- | --- |
| Creator 앞단 | 실행 기록·세션 기록에서 실패를 찾아 finding으로 만든다 (고정 ID, new·known·fixed·regressed·suppressed) |
| Creator | finding을 진단하고 수정 허용 범위 안의 개선안(SKILL 패치·마이너, Prompt persona 파생본, Agent 허용 필드)을 만든다. 새 SKILL 후보와 평가셋도 만든다 |
| Tester | 평가 샌드박스에서 기존·후보 버전을 같은 케이스로 짝지어 k회 실행한다 |
| Evaluator | 규칙 검사 → judge 채점 순으로 판정하고 버전을 비교한다. judge는 SEL이 고칠 수 없다 |
| Improver | 판정을 통과한 개선안을 새 버전(제안 상태)으로 등록·상신하고 승인을 요청한다. 활성 전환은 사람이 SEL Admin UI에서 승인할 때 일어난다 |

평가 샌드박스의 자산·도구 접근은 다른 창구와 같은 게이트(공통 진입점 → 자산 MCP Gateway → Tools Gateway / Database Connector)를 거친다.

일반 버전: [docs/images/sel-architecture.png](docs/images/sel-architecture.png) · 원본 spec: [docs/diagrams/](docs/diagrams/)

## 미결 (M0 게이트)

| # | 질문 |
| --- | --- |
| Q5 | 평가를 실제 도구(평가 계정, 조회 실제·쓰기 시험 실행)로 하나, 합성 세계로 하나 |
| Q6 | SEL이 읽는 실행 기록이 ARD 정본인가, Phoenix 사본인가 |
| Q7 | 평가 대상이 Runtime 안 업무 에이전트인가, 공용 자산인가. 후자면 운영 trace가 없어 운영 실패 원천은 세션 기록·사용 원장뿐이다 (가장 먼저 정한다) |
| Q8 | 등급 방향. 앱 설계 표기는 숫자가 클수록 민감으로 읽히고, 이 문서 전제는 1이 가장 민감이다 |

Q1–Q4(비운영 SAP, 평가 샌드박스 담당, 과거 버전 이력, v3 SEL 규격)는 [02 문서](docs/02_M0_questions_branches.md)에 있다.

## 문서

| 문서 | 내용 | 상태 |
| --- | --- | --- |
| [01 · SEL PRD](docs/01_PRD_SEL_v0.4.md) | 구조, Creator 앞단, 평가셋·평가 환경, Tester·Evaluator, Creator·Improver, 지표, 스케줄·이벤트·기한·용량, 마일스톤, 결정 요청 | v0.4 |
| [02 · M0 미결 질문과 설계 분기](docs/02_M0_questions_branches.md) | Q1–Q7과 답에 따른 설계 분기 | v0.3 |
| [03 · 트레이싱 PRD (참고)](docs/03_PRD_tracing_v3.1.md) | Collector 정본·사본 분리, 등급 기반 내용 제거. 트레이싱은 다른 팀 소관 | v3.1, 리뷰 결과 미반영 |
| [04 · e2e 개선 예시 14개](docs/04_SEL_e2e_examples.md) | 신호 → finding → 가설 → 후보 → 게이트 → 승인 → 검증이 어떤 논리로 일어나는지. 채택·불채택·이관 사례 | 참고 문서 (PRD 아님) |

## 실험장: [sel-lab/](sel-lab/)

LangGraph 기반 SEL 루프를 가짜 SAP 세계와 결함 주입 자산 위에서 처음부터 끝까지 돌린다. docker 없이 Python만 있으면 된다. 키가 없으면 오프라인 모드(규칙 기반 가짜 에이전트 + oracle Improver)로 배관만 검증하고, `.env`에 OpenAI 호환 Gateway 키를 넣으면 실제 모델로 돈다. 사용법은 [sel-lab/README.md](sel-lab/README.md).

## 핵심 설계 결정

- 구현은 **Tester·Evaluator를 먼저** 만든다. 하네스가 회귀를 놓치면 개선 루프가 회귀를 개선으로 채택하기 때문이다.
- 판정은 **게이트 G1–G4**로 한다: 목표 세트 개선(짝 차이의 신뢰 하한 > 0, MDI 이상), 가드 세트 비열등, 고정 사례 전부 통과, 판정 가능성(실패율 ≤ 10%, judge 보정). 케이스 단위 회귀 0을 보조 조건으로 둔다.
- 기밀 증거 내용이 후보 자산을 거쳐 밖으로 나가지 않게 **보안 경계 5지점**(등급 상속, 채널 상한, 누출 검사, 모델 고정, 증거 열람 제한)을 둔다.
- **judge는 고정**한다. judge 본문은 Prompt 자산이지만 SEL이 고칠 수 없다. 그래야 루프가 채점을 공략하지 못한다.
- 개선안은 **finding당 증분 패치 하나**만 만든다. release line은 하나다.
- **판정용 held-out은 사람이 관리**한다. SKILL 내장 평가 케이스와 Creator가 만든 평가셋은 진단용으로만 쓰고, SEL 패치는 SKILL의 평가 케이스 필드를 바꾸지 않는다. Creator가 자기 개선안을 판정할 케이스를 보고 맞추지 못하게 하기 위해서다.
- **활성 전환은 사람 승인으로만** 한다. 반영 후 운영 기록과 세션 기록으로 해소·재발을 확인하고, 점수만으로 fixed로 닫지 않는다.
- 평가 중 쓰기 본실행은 플래그가 아니라 **평가 계정 권한**으로 막는다.
