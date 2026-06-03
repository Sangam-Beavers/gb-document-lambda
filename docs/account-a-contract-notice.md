# 계정 A 백엔드 팀 전달 — AI 파이프라인 메타데이터 계약 확인/추가 요청

> 계정 B Lambda A ↔ 계정 A 백엔드(`gb-backend`, `DocumentSubmissionServiceImpl.buildS3Metadata()`) 간
> **S3 오브젝트 메타데이터 계약** 확인/합의 문서.
> 상위 문서: [`api-spec.md`](./api-spec.md) §1 (계약 A SSOT), [`result-queue-routing.md`](./result-queue-routing.md) §2, [`ai-pipeline.md`](./ai-pipeline.md) §6·§8.

## 배경

계정 B Lambda A는 클라이언트가 S3에 **직접 업로드**한 객체의 **메타데이터**만으로 개발기/운영기 결과 경로를 분기한다. 이 파이프라인엔 Lambda로 가는 HTTP hop이 없고(S3 ObjectCreated 이벤트 트리거), 클라이언트는 백엔드를 경유하지 않는다(PII 최소화). 따라서 백엔드가 Pre-signed PUT URL의 **서명 헤더에 심는 메타키가 유일한 계약 지점**이다.

> ✅ 참고: Lambda A `_read_metadata()`는 하이픈/언더스코어 양쪽 철자를 모두 수용하고, 누락 시 기본값(`user_lang→ko`, `analysis_document_type→UNKNOWN`)을 가진다. 따라서 아래 항목 중 **Lambda 코드 변경이 필요한 건 없다** — (a) 키 네이밍 정본 합의(문서), (b) 백엔드 메타키 2개 추가가 전부.

---

## ✅ 1. 구현 확인됨 — "이대로인지"만 확인

`buildS3Metadata()` + `RealS3PresignedUrlClient`(서명헤더 평탄화 → `upload_headers` 응답 포함)로 구현 확인됨.

| S3 메타키 (`x-amz-meta-*`) | 값 | 비고 |
| --- | --- | --- |
| `source` | `production` \| `development` | 프로필별 결정 (dev=development, stage·prod=production) |
| `document_id` | UUID | |
| `result_queue_arn` | SQS ARN | **`source=production`일 때만.** 자기 환경 큐 ARN(stage/prod) 통째로. dev는 빈 값 |

- `source`는 **2값뿐**(온프렘 vs AWS). stage/prod 구분은 `source`가 아니라 **`result_queue_arn`**가 담당 → Lambda는 환경 매핑 테이블 불필요.
- 클라이언트가 PUT 시 `upload_headers`를 **이름·값 그대로 echo**해야 함(누락 시 403). 구현 확인됨.

## 🔧 2. 백엔드 추가 필요 — 메타키 2개

### 2-1. `user_lang` — ⚠️ 우선순위 높음 (현재 silent 오번역 가능)

- Lambda B는 `user_lang`으로 **모국어 번역**을 하는데, B의 입력 출처는 A가 넘긴 메타데이터뿐(다른 조회 경로 없음).
- 현재 `buildS3Metadata()`에 **미구현** → Lambda가 조용히 `ko`로 기본 처리 → **비한국어 사용자 결과가 한국어로 잘못 번역**됨(에러 없이 틀리는 silent failure).
- **요청:** `buildS3Metadata()`에 `user_lang`(사용자 언어 선호) 추가. Lambda의 `ko` 기본값은 최후방 안전망으로 유지.

### 2-2. `analysis_document_type` — 저비용 (값 이미 보유)

- `ai-pipeline.md §5`가 Lambda B 입력으로 명시. Document 엔티티에 `analysisDocumentType` **값 보유 중**인데 메타데이터에 **미stamp**.
- 현재 미stamp 시 Lambda는 `UNKNOWN`으로 relay.
- **요청:** `buildS3Metadata()`에 `analysis_document_type = document.getAnalysisDocumentType()` 추가.

## 🔍 3. 키 네이밍 — **정본=언더스코어 확정**

- 백엔드 `buildS3Metadata()`·`api-spec.md`(SSOT)·Lambda 수용 키가 모두 **언더스코어** → **언더스코어를 정본으로 확정**(하이픈 표기였던 본 문서 초안·`CLAUDE.md §3-A` 정정 완료).
- Lambda는 양쪽을 받으므로 코드 변경 없음.
- ⚠️ **단 client→S3 presigned 직결 기준.** CloudFront/프록시 경유 시 언더스코어 헤더 드롭 가능 → 그땐 하이픈으로(Lambda 변경 불필요). **업로드 경로가 직결인지 확인** + 배포 후 Lambda A `raw object metadata keys` 로그로 실측 확정.

## ℹ️ 참고 — 분기 실패 시 동작 (계약 아님, 동작 통지)

- `source` 누락/모순(예: `production`인데 `result_queue_arn` 비어 있음) → Lambda는 **기본값 추측 없이 FAILED 처리**(오라우팅 데이터 오염 방지).
- S3 원본 삭제는 **Lambda A가 마스킹 직후 수행** → 백엔드 SQS Consumer는 원본 삭제 책임 없음(변경 없음).

---

## 액션 요약

| 항목 | 누가 | 작업 |
| --- | --- | --- |
| `source` / `document_id` / `result_queue_arn` | 계정 A | 확인만 (구현됨) |
| `user_lang` | **계정 A** | `buildS3Metadata()`에 추가 — **우선순위↑** (silent 오번역) |
| `analysis_document_type` | **계정 A** | `buildS3Metadata()`에 추가 (값 보유, 저비용) |
| 키 네이밍 = 언더스코어 | 공동 | 정본 합의 (문서 정정 완료) |
| 업로드 경로·키 round-trip | 공동 | presigned 직결 확인 + 배포 후 로그 실측 |
