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
| Evaluator | 규칙 검사로 판정하고 버전을 비교한다. judge는 게이트에 쓰지 않고 승인 화면 참고용이다(SEL이 고칠 수 없음) |
| Improver | 판정을 통과한 개선안을 새 버전(제안 상태)으로 등록·상신하고 승인을 요청한다. 활성 전환은 사람이 SEL Admin UI에서 승인할 때 일어난다 |

평가 샌드박스의 자산·도구 접근은 다른 창구와 같은 게이트(공통 진입점 → 자산 MCP Gateway → Tools Gateway / Database Connector)를 거친다.

일반 버전: [docs/images/sel-architecture.png](docs/images/sel-architecture.png) · 원본 spec: [docs/diagrams/](docs/diagrams/)

### SEL 상세 (스케줄러부터 검증까지)

![SEL 상세](docs/images/sel-detail-handdrawn.png)

매시 분석은 이미 쌓인 운영 기록을 세는 **관찰**이고, 매일 정기 골든셋은 활성 버전으로 고정 사례를 직접 돌려보는 **능동 시험**이다. 매일 기준선 재계산은 매시 분석이 "평소보다 나빠졌나"를 판단하는 잣대다. 자세한 표는 PRD 3장.

## 미결 (M0 게이트)

| # | 질문 |
| --- | --- |
| Q5 | 평가를 실제 도구(평가 계정, 조회 실제·쓰기 시험 실행)로 하나, 합성 세계로 하나 |
| Q6 | SEL이 읽는 실행 기록이 ARD 정본인가, Phoenix 사본인가 |
| Q7 | **v0.9에서 "공용 자산만"으로 진행** (Business·Personal 업무는 Runtime 밖 프론티어 도구). 운영 trace가 없어 운영 신호는 게이트 기록·세션 기록뿐, 운영 개선은 카나리로 판정. 설계자 확인만 남음 |
| Q8 | 등급 방향. 앱 설계 표기는 숫자가 클수록 민감으로 읽히고, 이 문서 전제는 1이 가장 민감이다 |

Q1–Q4(비운영 SAP, 평가 샌드박스 담당, 과거 버전 이력, v3 SEL 규격)는 [02 문서](docs/02_M0_questions_branches.md)에 있다.

## 문서

| 문서 | 내용 | 상태 |
| --- | --- | --- |
| [01 · SEL PRD](docs/01_PRD_SEL_v0.10.md) | 구조, Creator 앞단, 평가셋·평가 환경, Tester·Evaluator, Creator·Improver, 지표, 스케줄·이벤트·기한·용량, 마일스톤, 결정 요청 | v0.10 |
| [02 · M0 미결 질문과 설계 분기](docs/02_M0_questions_branches.md) | Q1–Q7과 답에 따른 설계 분기 | v0.3 |
| [03 · 트레이싱 PRD (참고)](docs/03_PRD_tracing_v3.1.md) | Collector 정본·사본 분리, 등급 기반 내용 제거. 트레이싱은 다른 팀 소관 | v3.1, 리뷰 결과 미반영 |
| [04 · e2e 개선 예시 14개](docs/04_SEL_e2e_examples.md) | 신호 → finding → 가설 → 후보 → 게이트 → 승인 → 검증이 어떤 논리로 일어나는지. 채택·불채택·이관 사례 | 참고 문서 (PRD 아님) |
| [05 · 설계 검토 기록](docs/05_SEL_design_review.md) | 게이트 오탐, 인젝션, confused deputy, Verifier, 리뷰 3차와 흐름 동결 | v0.8까지 반영 |
| [06 · 세부 설계 계획](docs/06_SEL_detailed_design_plan.md) | 동결된 흐름, **스케줄러 판단 로직**(작업별 입력·규칙·출력), 신호별 개선, 컴포넌트, 남은 빈틈, 빌드 순서 | v0.10 기준 |

## 실험장: [sel-lab/](sel-lab/)

LangGraph 기반 SEL 루프를 가짜 SAP 세계와 결함 주입 자산 위에서 처음부터 끝까지 돌린다. docker 없이 Python만 있으면 된다. 키가 없으면 오프라인 모드(규칙 기반 가짜 에이전트 + oracle Improver)로 배관만 검증하고, `.env`에 OpenAI 호환 Gateway 키를 넣으면 실제 모델로 돈다. 사용법은 [sel-lab/README.md](sel-lab/README.md).

## 핵심 설계 결정

- 구현은 **Tester·Evaluator를 먼저** 만든다. 하네스가 회귀를 놓치면 개선 루프가 회귀를 개선으로 채택하기 때문이다.
- 판정은 **게이트 G1–G4**로 한다: 목표 세트 개선(군집 보정한 신뢰 하한 > 0, 사전 고정 MMI 이상), 가드 세트 비열등, 고정 사례(결정적 정책 규칙은 위반 0, 나머지는 사례 수로 보정한 검정, k=10), 판정 가능성. 회귀 케이스는 "완전 붕괴 수 ≤ A/A 허용치"로 센다. 표본은 G1·G2가 동시에 검정력을 갖게 잡는다(목표 86, 가드 221). k·MMI·허용치는 A/A(같은 버전끼리)로 오판률 5% 이하가 되게 정하고, 그 전에는 실사용 케이스의 개선 루프를 시작하지 않는다.
- Verifier는 트리거 구간을 뺀 **"전" 구간 대비 유의한 감소**(대조군 이중차분)와 **급등 전 기준선 대비 비열등**으로 판정한다. 악화 판정은 순차 검정이고, 악화면 직전 승인 버전으로 **자동 롤백**(현재 활성이 릴리스한 버전일 때만, CAS)한다. 결론이 나지 않으면 "효과 미확인"으로 유지한다. 만성 결함은 신호별 절대 상한으로 잡는다.
- **보안 경계 6지점**: 나가는 쪽 5지점(등급 상속, 채널 상한, 누출 검사, 모델 고정, 증거 열람 제한)과 들어오는 쪽 1지점(유입 차단 = 증거 구획 + 유입 검사)이다. Creator·judge로 들어가는 외부 유래 텍스트는 예외 없이 증거 구획을 거친다.
- 셀프서비스 제출물은 **제출자 권한 ∩ 평가 계정 권한**으로 실행한다. 평가 계정은 op 허용목록, 행 범위, 등급 상한, rate limit, 결산기 차단으로 제한한다.
- **judge는 고정**한다. judge 본문은 Prompt 자산이지만 SEL이 고칠 수 없다. 그래야 루프가 채점을 공략하지 못한다.
- 개선안은 **finding당 증분 패치 하나**만 만든다. release line은 하나다.
- **사람은 최종 승인에만** 들어간다(SEL Admin UI). 그 밖의 루프 단계는 모두 자동이다: 골든셋은 SKILL 내장 평가 케이스와 결과 신호 재현 사례로 구성하고, judge는 승인 화면 참고용, 평가 케이스 결함은 자동 격리, HELD·INCONCLUSIVE는 자동 처리, REGRESSED는 직전 승인 버전으로 자동 롤백.
- **held-out = Creator가 한 번도 본 적 없는 골든셋 사례**다. 코드가 에이전트별 노출 원장을 두고, Creator 컨텍스트에 들어간 사례는 dev로 영구 강등한다. Creator에게 주는 자산 사본에서 평가 케이스를 지우고, 판정용 재현 사례는 증거로 쓰이지 않은 trace에서 만든다. 목표·가드는 코드가 뽑는다. 같은 finding의 판정은 총 3회까지다. SEL 패치는 SKILL의 평가 케이스 필드를 바꾸지 않는다.
- **활성 전환은 사람 승인으로만** 한다. 반영 후 운영 기록과 세션 기록으로 해소·재발을 확인하고, 점수만으로 fixed로 닫지 않는다. 사람 라벨이 없으므로 답의 값은 정답이 있는 사례에서만 검증된다는 한계가 있다.
- 평가 중 쓰기 본실행은 플래그가 아니라 **평가 계정 권한**으로 막는다.
