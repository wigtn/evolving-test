# CLAUDE.md

## 프로젝트

ERP AI 플랫폼의 SEL(self-evolving loop) 설계. 이 저장소의 본체는 **SEL ② 자산 생성**(Creator · Improver 알고리즘, 생성 근거, 거버넌스)이다. SEL 구조는 ① 입력 파이프라인 → ② 자산 생성 → ③ 평가 → ④ 반영이고, ①③④와 SEL 전체 설계는 다른 담당이다. 그쪽 내용은 "제안"으로만 쓴다.

- Creator: finding의 원인 가설 + 검증할 시험 문제(과거 실패 입력 + 정상 입력) 선정
- Improver: 가설 하나당 후보 하나, Registry에 candidate 등록
- ③ Evaluator는 LLM 판정자 쌍대 비교 + MDI 게이트, ④는 사람 승인 · Promoter · Verifier

## 틀리면 안 되는 사실

- 모든 에이전트는 플랫폼의 Agent Runtime(Aegra) 위에서 Harness(DeepAgents 기반)로 돈다. 실행 기록은 OTel로 Phoenix에 쌓이고, SEL 입력은 Phoenix API다. 높은 등급 본문 제거는 ① 전처리가 한다.
- Registry: draft → candidate → approved(사람만) + 릴리스 지정. SEL은 approved를 못 만든다.
- 바꿀 수 있는 것: 프롬프트, 스킬 본문, 도구 description, 매니페스트 파라미터(허용목록). 바꿀 수 없는 것: 도구 binding · required_capability · 스키마, Harness 1층, 권한 · 등급, 매니페스트 자산 참조, 확장 미들웨어, 스킬 평가 케이스, 판정 프롬프트.
- 평가 실행은 run_mode eval(쓰기 도구 Dry-Run, 모델 고정).
- 보안 등급은 1이 가장 민감하다(방향 확인 필요, Q8).
- 사내 LLM만 쓴다.

## 설계 불변식 (바꾸려면 docs/07 개정과 리뷰 필요)

- 후보 하나에 가설 하나, 변경 종류 하나, minor 버전. finding당 후보 3개까지.
- 시험 문제는 결정적 코드가 고른다. 노출 원장(Creator가 본 입력)은 영구히 시험 문제에서 빠진다. 판정 결과는 통과 여부만 돌려받는다.
- Creator에 들어가는 외부 텍스트는 증거 구획을 거친다. 후보는 거버넌스 · 유입 · 누출 · 등급 검사를 통과해야 등록한다.
- 사람 개입은 최종 승인 하나. 그 전 판단은 결정적 규칙.

## 문서

- `docs/07_SEL_2_asset_generation.md`: 본체.
- `docs/01_PRD_SEL_v0.11.md`: SEL 전체 참고안(제안 입력). 0장 정정 표가 본문보다 우선.
- `docs/06_SEL_detailed_design_plan.md`: 참고안. `docs/05`: 검토 기록. `docs/04`: e2e 예시. `docs/02`, `docs/03`: 이력.
- `sel-lab/`: 실험장. 가짜 데이터는 `python3 sel-lab/gen/build.py`. `data/ground_truth/`와 `data/defects/catalog.yaml`은 루프 코드에서 읽지 않는다. 키는 `.env`(커밋 금지).
- `docs/diagrams/`: 다이어그램 spec. png가 아니라 spec을 고쳐 다시 렌더한다.

## 작업 방식

- 한국어로 쓰고 기술 용어는 영어 그대로 쓴다.
- 모르는 건 모른다고 쓴다. 가정을 사실처럼 쓰지 않는다. 추천에는 근거와 트레이드오프.
- 빼는 최적화를 선호한다. 외부 자료는 버전, 출처, 확인일을 적는다.
- 큰 개정 뒤에는 독립 리뷰어 서브에이전트로 검증한다. 다이어그램은 손그림 스타일.

## 공개 저장소 규칙

- public 저장소다. 회사명, 사내 시스템명, 조직 · 인물 이름, 내부 문서 원문 · 링크 · id를 넣지 않는다. 사람은 역할(PM, ① 담당 등)로 쓴다.
- 이미지 안의 텍스트도 검사한다. 그대로 둬도 되는 이름: SAP, ARD, Aegra, OpenAI Dots, Oracle, Deep Tracer.
