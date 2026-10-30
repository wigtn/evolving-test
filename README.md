# evolving-test

사내 ERP AI 플랫폼의 **SEL(self-evolving loop)** 설계 문서 모음. 이 저장소의 본체는 **SEL ② 자산 생성(Creator · Improver · 거버넌스)** 설계다.

SEL은 실행 기록에서 자주 실패하는 자산(프롬프트 · 스킬 · 도구 설명 · 매니페스트 파라미터)을 찾아 고친 후보를 만들고, 같은 입력으로 비교 평가한 뒤 사람 승인을 거쳐 교체하는 루프다. 코드 배포 없이 자산 버전만 바꾼다. 모든 에이전트는 플랫폼의 Agent Runtime(Aegra) 위에서 Harness(DeepAgents 기반)로 돌고, 실행 기록은 OpenTelemetry로 Phoenix에 쌓인다.

## 구조

![SEL 위치와 파이프라인](docs/images/sel-architecture-handdrawn.png)

| 영역 | 하는 일 |
| --- | --- |
| ① 입력 파이프라인 | Phoenix API로 span을 가져와 전처리(높은 등급 본문 제거) · 분석하고 finding을 낸다 |
| **② 자산 생성 (이 저장소의 본체)** | **Creator**: 원인 가설을 세우고 검증할 시험 문제(finding과 같은 방식으로 틀리는 질문 + 정상 질문)를 만든다. **Improver**: 가설 하나당 후보 하나를 만들고 Registry에 candidate로 등록한다 |
| ③ 평가 | Tester가 기존 · 후보를 같은 시험 문제로 돌리고(eval 모드, 쓰기는 Dry-Run, 모델 고정), Evaluator가 LLM 판정자 쌍대 비교와 MDI 게이트로 판정한다 |
| ④ 반영 | 사람이 Admin UI에서 승인하면 Promoter가 릴리스 지정을 옮기고, Verifier가 7일 뒤 같은 패턴이 줄었는지 본다 |

SEL은 approved를 만들 수 없다. 활성 전환은 사람 승인으로만 일어난다.

### ② 자산 생성 상세

![② 자산 생성](docs/images/sel-detail-handdrawn.png)

LLM은 세 곳에서만 쓴다: 원인 가설(Creator), 시험 문제 생성(Creator 단계 안의 분리된 LLM, finding만 봄), 편집 명령(Improver). 증거 정리 · 시험 문제 검증(탐침) · 거버넌스 검사 · 등록은 결정적 코드가 한다. ① · ② 묶음 설계는 [docs/16](docs/16_SEL_1_2_design.md), ② 상세는 [docs/07](docs/07_SEL_2_asset_generation.md).

일반 버전: [docs/images/](docs/images/) · 원본 spec: [docs/diagrams/](docs/diagrams/) · 일정: [docs/images/sel-milestones.png](docs/images/sel-milestones.png)

## 문서

| 문서 | 내용 | 상태 |
| --- | --- | --- |
| [16 · SEL ① · ② 설계](docs/16_SEL_1_2_design.md) | **① 입력 파이프라인과 ② 자산 생성을 한 번에.** 아키텍처, finding 계약, 시험 문제 생성, 자가 개선, SEL 저장소, GEPA 도입 여부와 GEPA 아키텍처 | 초안 (2026-10-30) |
| [07 · SEL ② 자산 생성](docs/07_SEL_2_asset_generation.md) | **본체.** Creator · Improver 동작, 주고받는 것(finding · Registry 등록 · 평가 요청), 시험 문제 선정, 거버넌스, DSPy · GEPA 검토, 선행 협의 | 원칙 초안 v0.1 |
| [01 · SEL 전체 참고안](docs/01_PRD_SEL_v0.11.md) | SEL 전체를 놓고 쌓은 분석. ①③④ 담당과 PM에게 주는 제안 입력. 0장 정정 표가 본문보다 우선 | 참고안 v0.11 |
| [06 · 세부 설계 계획 (참고)](docs/06_SEL_detailed_design_plan.md) | 스케줄러 판단 로직, 신호별 개선, 컴포넌트 | 참고안 |
| [05 · 설계 검토 기록](docs/05_SEL_design_review.md) | 게이트 오탐, 인젝션, confused deputy, Verifier, 리뷰 기록 | 이력 |
| [04 · e2e 개선 예시 14개](docs/04_SEL_e2e_examples.md) | 신호 → finding → 가설 → 후보 → 판정 → 승인 → 검증 예시 | 참고 |
| [02 · 미결 질문과 설계 분기](docs/02_M0_questions_branches.md) | 초기 미결 질문 (일부는 기준 문서로 닫힘) | 이력 |
| [03 · 트레이싱 PRD (참고)](docs/03_PRD_tracing_v3.1.md) | 다른 팀 소관. Collector 구조로 쓰여 있어 지금 구성과 다름 | 이력 |

## 미결

| # | 질문 |
| --- | --- |
| ②-1 | Creator 단계 안에서 가설 LLM과 시험 문제 생성 LLM을 분리해 구현해도 되는지 (PM 확인) |
| ②-2 | SEL이 바꿀 수 있는 매니페스트 파라미터 범위(안전 파라미터 제외) |
| ②-3 | ③이 ②에 돌려주는 결과 범위(통과 여부만) |
| ②-4 | MDI 게이트 정의와 사전 고정 최소 의미 개선 폭 |
| ②-5 | finding당 후보 3개 상한 |
| Q8 | 보안 등급 방향(1이 가장 민감인지) |

## 실험장: [sel-lab/](sel-lab/)

LangGraph 기반 SEL 루프를 가짜 SAP 세계와 결함 주입 자산 위에서 처음부터 끝까지 돌린다. docker 없이 Python만 있으면 된다. 키가 없으면 오프라인 모드(규칙 기반 가짜 에이전트 + oracle Improver)로 배관만 검증하고, `.env`에 OpenAI 호환 Gateway 키를 넣으면 실제 모델로 돈다. 사용법은 [sel-lab/README.md](sel-lab/README.md).

## 핵심 설계 결정 (② 자산 생성)

- **무엇을 왜 바꿔도 되는지**가 핵심이다. 바꿀 수 있는 것: 프롬프트, 스킬 본문, 도구 description, 매니페스트 파라미터(허용목록). 바꿀 수 없는 것: 도구 binding · required_capability · 스키마, Harness 1층, 권한 · 등급, 매니페스트의 자산 참조, 확장 미들웨어, 스킬 평가 케이스, 판정 프롬프트.
- **후보 하나에 가설 하나.** 증분 패치, 변경 종류 하나, minor 버전. 같은 finding에 후보는 3개까지.
- **시험 문제는 생성하고 실행으로 거른다.** 분리된 생성 LLM이 finding(패턴 · 서명 · hint)만 보고 질문을 만들고(가설 · 후보는 못 봄), 하네스가 지금 버전으로 2번씩 돌려 같은 서명으로 틀리는 것과 2번 다 통과하는 것만 남긴다(1번만 틀리면 3번째로 정함). 필수 · 정책 케이스는 사람이 만든 고정 세트. 판정 결과는 통과 여부만 돌려받는다.
- **Creator는 사용자 질문을 보지 않는다.** 실행 구조(도구 이름, 인자 이름, 결과 코드, 결과 건수, 재요청 같은 이벤트)만 넘긴다. 후보는 유입 검사(안전 절차 줄 삭제 · 완화도 실패) · 누출 검사 · 등급 검사를 통과해야 등록된다.
- **생성 근거를 버전에 남긴다**: finding ID, 가설, 변경 전후 차이, 시험 문제 목록, 기준 버전, 증거 등급, 검사 결과, 사용 모델.
- 사람 개입은 ④의 최종 승인 하나다. 그 전 판단은 결정적 규칙이고, 판단할 수 없으면 승인 요청 없이 끝낸다.
- 사내 LLM만 쓴다.
