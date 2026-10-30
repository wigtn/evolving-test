# SEL e2e 예시 14개: 개선이 어떤 논리로 일어나는가

작성 2026-10-27 · 참고 문서 (PRD 아님)

> **상태 (확인 2026-10-30, 위키 · 기준 문서 2026-10-28 기준): 이력 예시.** 흐름의 논리(가설 → 시험 → 승인 → 반영 → 확인)는 참고로 남기되 아래는 낡았다.
> - "운영 trace가 없을 수 있다(PRD Q7)"는 철회. 모든 에이전트가 우리 Runtime에서 돌아 운영 실행 기록이 다 있다. 원천은 Gateway 원장 · 세션 기록이 아니라 Phoenix API(① 입력 파이프라인이 finding을 만듦).
> - 역할: Creator = 원인 가설 + 시험 문제 선정, Improver = 가설 하나당 후보 하나를 Registry candidate로 등록(②). Tester · Evaluator(LLM 판정자 쌍대 비교 + MDI 게이트)는 ③, 사람 승인 · Promoter · Verifier(7일 뒤 확인)는 ④. 위 G1–G4 게이트와 케이스 상태(TRIAGED · CANARY 등)는 제안이다.
> - 기준 문서에 카나리는 없다. Promoter는 승인된 버전으로 Registry 릴리스 지정을 옮긴다(다음 요청부터 새 버전). 자산 상태는 draft → candidate → approved(사람만) → deprecated → retired, candidate → rejected(평가 미달 · 반려).
> - 없던 자산을 새로 만드는 경우(R7 no_asset_match 예시)는 기준 문서에 명시가 없다. 기준 문서 ②의 예시는 기존 자산 수정(프롬프트 · 도구 설명 · 스킬 본문)이다.

SEL 애플리케이션 설계 v0.1(2026-10-23)의 용어를 따른다. 업무 내용과 수치는 모두 가상이다. 예시 대부분은 `sel-lab/data/defects/catalog.yaml`의 결함(D01–D10)으로 재현할 수 있다.

## 공통 흐름

```
신호(R1–R8) → finding → 케이스 OPEN
  → Creator: 가설·대상·평가 세트(목표·가드·고정)            TRIAGED
  → Improver: 후보 버전 (변경 유형 안에서) + 정적 검사        CANDIDATE_READY
  → Tester: 기준 팔·후보 팔 쌍대 실행 k회                     EVALUATING
  → Evaluator: 규칙 채점 + 채점표 judge
  → Gate: G1 개선 · G2 비열등 · G3 필수 사례 · G4 판정 가능성  GATE_PASSED / GATE_FAILED / HELD
  → 사람 승인 (Admin UI)                                      AWAITING_APPROVAL → APPROVED
  → Promoter: 카나리 또는 전체 릴리스                          CANARY / RELEASED
  → Verifier: 반영 후 같은 패턴 발생률 비교                    VERIFYING → VERIFIED / REGRESSED
```

역할 경계에 대한 주의: 플랫폼 설계 방향(2026-10-22)은 Improver를 "등록·상신"으로, SEL 애플리케이션 설계(2026-10-23)는 "후보 버전 생성"으로 적고 있다. 이 문서는 애플리케이션 설계를 따른다. PRD v0.3은 플랫폼 설계 방향을 따랐으므로 둘 중 하나로 맞춰야 한다.

운영 trace가 없을 수 있으므로(PRD Q7), 예시마다 신호 원천을 적었다. 원천은 Gateway 원장, 세션 기록, 평가 실행 세 가지다.

---

## 채택되는 경우

### 1. 기간 누락 → row limit 초과 (D01)

| 단계 | 내용 |
| --- | --- |
| 신호 | R1 tool_error. 원천: Tools Gateway 감사 기록. `fi-open-items-query`의 ROW_LIMIT_EXCEEDED가 기준선의 1.5배 이상 |
| finding | target `skill/fi-open-items@1.4.2`, hypothesis_class `missing_instruction`. 지지 20건 이상, 서로 다른 주체 3명 이상이라 케이스 개설 |
| Creator | 가설: "기간이 없을 때 기본값 규칙이 없어 전체 기간을 조회한다." 목표 세트는 `open_items_no_period`, 가드 세트는 `open_items_annual` |
| Improver | 변경 유형 instruction. SKILL 절차에 당월 기본값 규칙 추가 (30줄 이내, 섹션 단위 diff) → `@1.4.3` |
| Tester·Evaluator | 규칙 채점: 도구 인자에 `period_from=당월`, ROW_LIMIT 없음, 근거 일치 |
| Gate | G1 d̄ 하한 > 0 그리고 ≥ MDI, G2 가드 비열등, G3 통과 |
| 이후 | 승인 → full 충실도라 전체 릴리스 → Verifier가 7일 전후로 ROW_LIMIT 발생률을 비교 → VERIFIED |

### 2. 거래처 모호 → 첫 결과로 진행 (D02)

- **신호**: R5 negative_feedback. 세션 기록에서 "그 거래처 말고" 재요청이 반복된다. trace에는 보이지 않는 신호다.
- **Creator**: 가설 "검색 결과가 2건 이상일 때 묻는 단계가 없다." 목표 세트는 `open_items_ambiguous`다.
- **Improver**: instruction 변경으로 되묻기 단계를 추가한다.
- **판정**: user simulator가 "서울지점이요"라고 답하는 대화 케이스로 판정한다(M2b). 규칙은 되묻기 발생과 최종 거래처 ID 일치다.
- **이후**: VERIFIED는 세션 기록의 재요청 감소로 판정한다.

### 3. 금액 단위 혼동 (D03)

- **신호**: R5. 사용자가 "4,821만"을 "48,210,000원"으로 고쳐 쓴다.
- **Improver**: 대상은 Prompt persona다. 변경 유형은 instruction이다(앱 설계 6.2는 prompt 본문 instruction을 허용).
- **판정**: format 기준은 규칙(숫자 정규화), scope_stated는 judge로 본다.
- **이후**: 전체 릴리스 후 사용자 수정 비율 감소로 VERIFIED.

### 4. 쓰기 전 승인 요청 누락 (D04)

- **신호**: R6 eval_fail. 정기 골든셋의 `document_post` 고정 세트에서 승인 요청 케이스가 실패한다.
- **Improver**: instruction 변경으로 "dry_run → 승인 요청 → 토큰으로 본실행" 단계를 복원한다.
- **판정**: G3 필수 사례에서 기준 팔이 통과한 규칙 채점을 후보 팔이 하나라도 실패하면 안 된다. 쓰기는 Dry-Run까지만 한다.
- **결과**: 채택된다. 지연이 늘지만 budget_exceeded 임계 안이다.

### 5. 턴 수 부족으로 중단 (D05)

- **신호**: R8 budget_exceeded. 다단계 요청(`multi_vendor`)이 `max_turns` 4에서 끊긴다.
- **Improver**: composition 변경으로 Agent의 허용 필드 `max_turns`를 4에서 12로 바꾼다.
- **판정**: G1으로 개선을 확인한다. 동시에 케이스당 토큰이 예산 안인지 본다. 예산을 넘으면 G4 판정 가능성 또는 비용 기준에서 막힌다.

---

## 채택되지 않는 경우 (가드가 작동하는 예)

### 6. 지연을 줄이려다 안전 행동이 빠짐 (D04 변형)

- **후보**: Creator가 "응답 지연" finding에 대해 "확인 질문을 생략한다"는 가설을 내고, Improver가 그 후보를 만든다.
- **판정**: 목표 세트는 개선된다. 그런데 고정 세트(`document_post`, SME 필수)에서 승인 요청 케이스가 실패한다.
- **결과**: **G3 위반으로 GATE_FAILED**다. 개선 압력이 안전 행동을 깎는 것을 막는 장치가 G3다.

### 7. 한 곳을 고치다 다른 곳이 깨짐 → 반복 2 (D01 변형)

- **1차 후보**: "기간이 없으면 항상 당월"로 고쳤다. 그 결과 가드 세트(`open_items_annual`: "올해 누적", "전체 기간")까지 당월로 잘린다.
- **1차 판정**: G2 비열등 위반으로 GATE_FAILED. 케이스는 TRIAGED로 돌아간다(반복 ≤ 3).
- **2차**: Creator가 가설을 "기간 표현이 전혀 없을 때만 당월"로 좁히고, Improver가 새 후보를 만든다.
- **2차 판정**: G1·G2 통과. 반복마다 유의수준을 α' = 0.05 / 3으로 나눠 쓴다.

### 8. 노이즈 속 착시 (대조군 C00)

- **상황**: 결함 없는 자산에 케이스를 억지로 열었다. 후보의 평균 점수는 조금 올랐다.
- **판정**: d̄의 단측 신뢰 하한이 0 이하이거나 MDI 미만이다.
- **결과**: **G1 미통과로 GATE_FAILED**. 반복 3회 동안 통과하지 못하면 REJECTED다. 평균이 올랐다는 것만으로는 개선이 아니다.

### 9. 목표 세트에서 기준 팔이 이미 통과 중

- **상황**: 재현 사례가 부족해서, 목표 세트를 기존 골든셋으로만 채웠다.
- **판정**: 기준 팔이 거의 다 통과하므로 d̄가 0 근처다. G1을 통과할 수 없다.
- **처리**: Creator는 증거가 부족한 케이스를 DISMISSED로 닫는다(사유 기록). 또는 증거 trace의 재현 사례를 SME 라벨 대기열에 올려 골든셋이 실사용 분포를 따라 자라게 한다.

### 10. judge 공략

- **후보**: Improver 후보가 답을 길고 채점표 단어가 많게 만든다.
- **판정**: judge는 쌍대 판정에서 순서를 바꿔 두 번 묻는다. 두 판정이 다르면 무승부다. 게다가 judge는 Improver와 다른 모델·프롬프트이고 SEL이 고칠 수 없다. 규칙 채점(근거 일치)에서 숫자 하나가 틀린다.
- **결과**: 사례 점수는 규칙 0.5 + judge 0.5라서 오르지 않는다. 승인 화면에서 diff와 증거를 보면 장황함이 보인다. 반려된다.

---

## SEL이 고치지 않고 넘기는 경우

### 11. 도구 장애 (D07)

- **신호**: `fi-open-items-query` timeout이 15% 증가한다.
- **분류**: 실패 원인이 플랫폼(llm_unavailable, dbc_saturated 등)이다. 자산 finding에서 빠지고 플랫폼 신호로 따로 집계된다(앱 설계 5.2).
- **결과**: 케이스가 열리지 않는다. 열렸다면 Creator가 "가설이 대상 자산 밖(플랫폼)을 가리킨다"는 사유로 DISMISSED한다.

### 12. 평가 케이스 라벨이 틀림 (D08)

- **상황**: `gl_balance/0003`의 정답이 환산 전 값으로 잘못 들어가 있다.
- **증상**: 후보 팔과 기준 팔이 모두 실패한다. 실행 실패가 아니라 라벨과 불일치하는 것이다.
- **처리**: SEL은 eval_case를 고치지 않는다. eval_set은 실행 전에 고정되고 이후 변경할 수 없다. SME 라벨 수정은 `/v1/eval-cases/{id}:label`로 한다.
- **결과**: 수정된 라벨로 다음 반복의 eval_set을 새로 고정한다.

### 13. 덮는 자산이 없는 반복 절차 → 새 SKILL (D09)

- **신호**: R7 no_asset_match. `skill.search`가 0건이고, 사용자 U9003·U9004·U9008이 "월말 거래처 미결 대사"를 매번 손으로 반복한다(세션 기록).
- **finding**: target `asset_ref=null`, proposed_kind `skill`.
- **Creator·Improver**: change_type `new_asset`이다. 신규 SKILL 초안과 평가 세트를 만들고, 사람 검토 필수 표시를 붙인다.
- **릴리스**: 카나리 없이 전체 릴리스하는 것은 금지된다. new_asset은 카나리로 시작한다(앱 설계 6.6).

### 14. 반영 후 재발 → 롤백

- **상황**: 예시 2의 후보가 RELEASED된 뒤, 다른 경로로 활성 버전이 바뀐 자산과 겹쳤다. Verifier가 7일 전후 비교에서 재요청 발생률 악화를 검정한다.
- **처리**: REGRESSED가 되고, 사람 승인 후 Promoter가 롤백한다. 버전은 불변이라 직전 승인 버전으로 전환만 하면 된다.
- **비교**: 카나리 단계였다면 악화가 유의할 때 자동 롤백된다. 노출 범위가 작고 되돌릴 대상이 이미 승인된 직전 버전이기 때문이다.

---

## 예시에서 보이는 논리

- 개선의 근거는 Gateway 원장, 세션 기록, 평가 실행이다. 운영 trace가 없어도 루프는 돈다.
- 채택은 평균 점수가 아니라 **G1 개선(신뢰 하한, MDI) + G2 비열등 + G3 필수 사례 + G4 판정 가능성**이 결정한다(예시 6–10).
- SEL이 고칠 수 없는 것은 넘긴다: 플랫폼 장애(11), 평가 라벨(12), judge(10).
- 끝은 게이트가 아니라 Verifier의 반영 후 비교다(1, 2, 14).

## 결함 ↔ 예시 대응

| 결함 | 예시 | 재현에 필요한 것 |
| --- | --- | --- |
| D01 | 1, 7 | heavy 거래처 3곳, `open_items_no_period`, `open_items_annual` |
| D02 | 2 | 이름이 겹치는 거래처 그룹, user simulator |
| D03 | 3 | persona의 amount_format 규칙 |
| D04 | 4, 6 | `document_post` 고정 세트 |
| D05 | 5 | `multi_vendor` |
| D06 | (R2 예시) | `gl_balance` "9월", "지난달" 표현 |
| D07 | 11 | 도구 장애 주입 |
| D08 | 12 | `gl_balance/0003` 라벨 오염 |
| D09 | 13 | persona의 `month_end_vendor_recon` |
| D10 | (wrong_example) | `open_items_multi_currency` |
| C00 | 8 | 결함 없는 정상 자산 |
