# CLAUDE.md — gb-document-lambda

AI 서류 분석 파이프라인 **Lambda 코드** 레포. (handoff 문서의 `sb-ai-pipeline` 가칭과 동일 레포)

## 0. 이 레포의 정체 (가장 먼저 읽을 것)

- **소속:** AWS **계정 B**(AI 분석 전용 격리 계정). 계정 A(gb-backend, Java/Spring)와 분리.
- **범위:** **Lambda A + Lambda B 두 개만.** 그 외는 모두 다른 레포/팀 소관.
- **언어/런타임:** **Python 3.12**, 의존성은 사실상 `boto3` 하나.
- **배포:** **ZIP** (컨테이너 이미지/ECR 아님 — 전처리 라이브러리를 안 쓰기로 확정).
- **현재 상태:** 코드 미작성. `docs/`와 README만 존재. 첫 구현 대상.

### 이 레포에 **없는 것** (건드리지 말 것)
- `POST /api/v1/documents` Pre-signed URL 발급, SQS Consumer, 조회 API → **계정 A gb-backend(Java)** 소관.
- 후속 질문 **챗봇 Lambda · MCP Server** → 또 다른 별도 레포.
- DynamoDB(챗봇 대화기록), 챗봇 Redis → 챗봇 레포 소관.

## 1. 두 Lambda의 책임

| | Lambda A | Lambda B |
| --- | --- | --- |
| 역할 | 텍스트 추출 + PII 마스킹 | 법령 RAG 분석 + 번역 + 결과 경로 분기 |
| 트리거 | S3 ObjectCreated (업로드 버킷) | Lambda A의 **비동기** invoke(`InvocationType="Event"`) |
| 모델 호출 | Bedrock Claude Vision **1회** (추출+마스킹 동시) | Bedrock Claude **Tool Use 루프** + KB `retrieve` |
| 타임아웃 | **300s** | **600s** |
| 메모리 | 512MB~1024MB 시작, 측정 후 조정 | 동일 |

### Lambda A 처리 순서
1. `event["Records"]`에서 bucket/key 파싱
2. `s3.head_object`로 메타데이터 읽기 (`source`, `document_id`, `result_queue_arn`, `user_lang`)
3. `s3.get_object`로 원본 바이트 로드
4. Bedrock Converse API 1회 호출 (추출 + `[항목-마스킹]` 동시, 표/조항번호 보존)
5. **원본 바이트 변수 즉시 `del`** (PII Layer 2 — 메모리 잔존 최소화)
6. 마스킹 텍스트를 `masked/` S3 키에 업로드 → `masked_file_url`
7. **`original/` S3 원본 삭제** (PII Layer 3 — 마스킹본 저장 성공 직후. best-effort, 실패해도 7일 수명주기 백스톱)
8. **Lambda B 비동기 invoke** 후 즉시 종료 (B 완료 안 기다림)

> 원본 삭제를 A가 맡는 이유: B는 페이로드 `masked_text`(+`masked_file_url`)로만 분석하므로 원본이 더 이상 필요 없다. 마스킹본을 먼저 영속화한 뒤 원본을 지우므로 데이터가 마스킹본 없이 사라지지 않는다. 마스킹 **성공 후** 삭제이므로, 메타 누락·VLM 실패(삭제 이전 단계)는 여전히 원본을 유지한다.

### Lambda B 처리 순서
조항 분해 → Tool `get_legal_standard` 호출 시 KB `retrieve` → 조항↔법령 비교로 위험 항목·등급 산출 → 급여 요약 → 모국어(`user_lang`) 번역 → 결과 JSON 조립 → **`source`에 따라 한 경로로만 전송** 후 종료. **S3 원본 삭제는 A가 이미 수행**(B는 원본 미보유).

## 2. Bedrock 모델 ID (실수 잦은 지점)

- 기준 모델: **`anthropic.claude-sonnet-4-6`** (Vision + Tool Use, A·B 공통).
- ⚠️ 서울 리전(`ap-northeast-2`)은 **foundation model ID 직접 호출 불가 → inference profile 필수.**
  - **확정값(2026-06 동작 확인): `global.anthropic.claude-sonnet-4-6`** — 코드 기본값·`BEDROCK_MODEL_ID`에 사용.
  - **`us.` 접두사 금지** (`400 invalid model identifier`). `apac.`도 가능하나 검증된 건 `global.`.
  - ✅ **Lambda A 런타임 검증 완료(2026-06):** 실제 Lambda에서 Converse Vision 호출 → OCR 추출 + PII 마스킹 정상 동작 확인.
  - ⬜ 미검증: **Lambda B 런타임**(Tool Use 루프·KB 코드 호출·dispatch), **A→B invoke**, end-to-end.
- 법령 RAG: **Bedrock Knowledge Bases `retrieve`** (`bedrock-agent-runtime`). KB 백엔드 저장소 = **S3 Vectors**. 코드에서 S3 Vectors/임베딩 모델을 직접 만지지 않는다 — KB가 임베딩·벡터질의·조립을 대신함.

## 3. 계약(Contract) — 깨면 양쪽 다 깨짐

### A. S3 오브젝트 메타데이터 (계정 A 백엔드 → Lambda A 입구)
백엔드가 Pre-signed URL 서명 헤더에 심음. Lambda A는 `head_object`로 읽되 **`x-amz-meta-` 접두사가 빠진 채** 나온다.

| S3 wire 키 | head_object Metadata 키 | 값 |
| --- | --- | --- |
| `x-amz-meta-source` | `source` | `production` \| `development` |
| `x-amz-meta-document_id` | `document_id` | UUID |
| `x-amz-meta-result_queue_arn` | `result_queue_arn` | SQS ARN (`source=production`일 때만) |
| `x-amz-meta-user_lang` | `user_lang` | `ko` 등 (⬜ 백엔드 미stamp — 추가 필요) |

> **정본=언더스코어** (백엔드 `buildS3Metadata()`·`api-spec.md` SSOT와 일치). Lambda A `_read_metadata()`는
> 하이픈/언더스코어 양쪽을 받으나(`pick(...)`), **계약 표기는 언더스코어로 통일**한다.
> ⚠️ 단 client→S3 presigned **직결** 기준. CloudFront/프록시 경유 시 언더스코어 헤더가 드롭될 수 있음 →
> 그땐 하이픈으로(Lambda 코드 변경 불필요, 양쪽 수용). 배포 후 `raw object metadata keys` 로그로 실측 확정.
> ⬜ **백엔드 미stamp(추가 필요):** `user_lang`(없으면 B가 `ko`로 silent 기본 → 비한국어 오번역, 우선순위↑),
> `analysis_document_type`(Document 엔티티에 값 보유, §3-B).

### B. Lambda A → Lambda B 페이로드
```json
{ "document_id":"uuid", "s3_key":"uploads/...", "masked_text":"...",
  "masked_file_url":"s3://.../masked/...", "user_lang":"ko",
  "source":"production", "result_queue_arn":"arn:aws:sqs:...:gb-analysis-results-prod",
  "analysis_document_type":"LABOR_CONTRACT" }
```
> `result_queue_arn`을 A가 B로 **relay**해야 함 (production 경로 발행에 필요).
> ⚠️ **미확정 계약:** `analysis_document_type`은 `ai-pipeline.md §5`가 B 입력으로 명시하나
> handoff §6 메타데이터 표엔 없음. 현재 코드는 A가 `x-amz-meta-document-type`(또는
> `analysis-document-type`)을 읽어 relay하고, 없으면 `UNKNOWN`. **백엔드가 이 메타키를 심도록 합의 필요.**

### C. Lambda B 결과 전송 — `source`로 **택일**(동시 전송 아님)
- `source=production` → **`result_queue_arn` 큐로 SendMessage** (백엔드가 심은 ARN 그대로 사용. Lambda는 환경 매핑 테이블 불필요).
  - 큐는 `gb-analysis-results-stage` / `gb-analysis-results-prod`로 **물리 분리**. 각 환경 백엔드가 자기 큐만 구독.
  - **페이로드 위치 규약:** 결과 JSON은 **SQS 메시지 본문 그대로**, 라우팅 메타(`source`, `document_public_id`)는 **SQS MessageAttributes**로.
- `source=development` → VPC 라우트(10.10.1.0/24 → WireGuard EC2) → 터널 → **온프렘 개발기 MySQL 직접 INSERT**. (**HAProxy 미사용** — EC2는 WireGuard 터널 엔드포인트일 뿐.)

> `source`는 `production`/`development` **2값뿐.** stage 전용 값 없음(stage·prod 모두 `production`, 어느 Aurora인지는 큐가 가름). 챗봇의 3값 `environment`와는 **별개 필드** — 합치지 말 것.

### 결과 JSON 컬럼 매핑 (백엔드 `document_results`에 들어감)
```
processing_status, overall_risk_level, ocr_confidence,
wage_summary{currency_code, monthly_wage, hourly_wage, deductions[]},
risk_items[]{risk_level, clause, description},
translated_text, masked_file_url, failed_reason, completed_at
```

## 4. PII 3-Layer (반드시 지킬 것 / 개인정보보호법 §16)

1. 수집 최소화 — 클라이언트가 S3 직접 업로드(백엔드 미경유). *백엔드 책임, 이 레포 무관.*
2. 처리 중 마스킹 — Claude VLM 1회 추출+마스킹, **원본 메모리 즉시 소멸**(`del`). → **Lambda A 책임.**
3. 사후 삭제 — 마스킹본 S3 저장 직후 **`original/` S3 원본 삭제**. → **Lambda A 책임** (B 분석은 `masked_text`만 쓰므로 원본 불필요 → 최대한 일찍 삭제).

마스킹 대상: 이름·주민등록번호·외국인등록번호·전화번호·주소·계좌번호.

## 5. 인프라 메모

- VPC `sb-ai-vpc` (10.110.0.0/16). Lambda는 **관리(프라이빗) 서브넷** `sb-mgmt-subnet-a/c`.
- 필요 VPC Endpoint: **Bedrock(`bedrock-runtime`+`bedrock-agent-runtime`), S3 Gateway, SQS.** ~~ECR~~(ZIP 배포로 제거).
- DB 서브넷 없음(Aurora 제거, 법령=S3 Vectors).
- Lambda A IAM: `s3:GetObject/PutObject/DeleteObject`, `bedrock:InvokeModel`, `lambda:InvokeFunction`(B만), `logs:*`. (`DeleteObject`는 PII Layer 3 원본 삭제용 — A로 이관.)
- Lambda B IAM: `bedrock:InvokeModel`, `bedrock:Retrieve`, `sqs:SendMessage`(계정 A 큐 — 크로스계정 정책), EC2 네트워크 경로(dev), `logs:*`. (~~`s3:DeleteObject`~~ — 원본 삭제 A 이관으로 제거.)

## 6. 디렉터리 구조 (현재)
```
lambda_a/handler.py + requirements.txt   # 추출+마스킹 (구현됨, 데모 수준)
lambda_a/_smoke_test.py                  # boto3 스텁 순수헬퍼 테스트 (python lambda_a/_smoke_test.py)
lambda_b/handler.py + requirements.txt   # RAG 분석 + 경로 분기 (구현됨, 데모 수준)
lambda_b/_smoke_test.py                  # 동일
infra/                                   # (선택) Terraform/CDK — 미생성
```
- Lambda A 의존성: 없음(boto3 내장). Lambda B: `PyMySQL`(development 경로 MySQL INSERT 전용, 지연 import).
- 핸들러 진입점: 둘 다 `handler.handler`.
- 환경변수 키는 각 handler 상단 참고 (`BEDROCK_MODEL_ID`, `LAMBDA_B_NAME`, `LEGAL_KB_ID`, `UPLOAD_BUCKET`, `ONPREM_DB_*` 등).

### 운영 전환 전 확정할 TODO (코드 주석에 표시됨)
- A: `source`/`document_id`·VLM 실패 시 **FAILED 기록**(현재 데모는 로그+예외만, SQS/DB 권한 없음).
- A: 배포 후 `head_object` 실제 메타키 이름 로그 확인 → 백엔드와 1:1 통일.
- B: ✅ 온프렘 스키마 대조 완료(2026-06-04, 설계 문서 기준) — `document_results`는 `submission_id`(BIGINT NOT NULL UNIQUE FK, `document_public_id` 컬럼 없음) → public_id→id 선조회. 마스킹본은 `s3_masked_key`(경로만, s3://버킷 접두사 제거). `completed_at` DATETIME NOT NULL(ISO→`YYYY-MM-DD HH:MM:SS` 변환, FAILED면 현재시각 대체). `analysis_document_type` 컬럼은 results에 없음(submissions `document_type` 소유). **`document_submissions.status`도 B가 함께 갱신**(2026-06-05 수정 — FAILED→FAILED, COMPLETED/PARTIAL→COMPLETED, 운영 SQS Consumer와 동일 매핑. 이전의 "UPLOADED/SENT_TO_AWS/FAILED_UPLOAD 전용이라 안 건드림" 전제는 오류: 백엔드 실제 enum은 ANALYZING/COMPLETED/FAILED이고 GET /status 폴링이 이 컬럼만 읽어서, 갱신 없이는 프론트가 영원히 "분석 중"에 머묾). MySQL 연결은 **평문 고정 — `ssl_disabled=True` 필수**(ssl 인자 "생략"만으론 부족: PyMySQL 1.2.0은 미지정 시 PREFERRED 모드라 서버가 SSL 광고하면 TLS 시도 → WireGuard MTU에서 핸드셰이크 행. caching_sha2는 평문에서도 공개키 교환으로 자동 인증).
- B: ✅ KB `retrieve` 실동작 확인(2026-06). 남은 건 `LEGAL_KB_ID`·`UPLOAD_BUCKET` env 주입뿐.
- A·B 공통: `analysis_document_type` 메타키 백엔드 합의(§3-B).

## 7. Day 1 PoC 순서 (진행 현황)
1. ✅ inference profile ID 확정 → `global.anthropic.claude-sonnet-4-6` (2026-06).
2. ✅ 법령 KB 적재 + Bedrock 콘솔에서 retrieve 동작 확인.
3. ✅ **Lambda A 런타임 검증 완료** — 콘솔 Test(합성 S3 이벤트, 수동 메타데이터)로 Converse Vision 호출 → OCR + PII 마스킹 정상.
4. ⬜ **Lambda B 배포 → Tool Use 루프·KB retrieve를 Lambda 코드로 실행 확인.** (다음 우선순위)
5. ⬜ A→B 비동기 invoke 발사 확인(`LAMBDA_B_NAME` 채워서).
6. ⬜ S3 ObjectCreated 트리거 연결(업로드 자동 발화) — prefix 필터 `original/`로 재귀 방지.
7. ⬜ (병행) 백엔드와 메타데이터 키 이름 1:1 매칭 + 로그 검증.

> 배포·AWS 검증은 유저가 AWS 콘솔/CLI에서 직접 수행(이 머신엔 AWS CLI·자격증명 없음). 로컬 순수 로직은 `_smoke_test.py`로 검증 완료.

## 8. 문서 (이 레포 `docs/`)

- `docs/sb-ai-pipeline-handoff.md` — **이 레포의 1차 사양**(Lambda A/B 핸드오프).
- `docs/ai-pipeline.md` — 분석 파이프라인 정본(계정 B 전체 흐름).
- `docs/architecture.md` — 계정 A/B 분리, VPC, 보안 원칙.
- `docs/api-spec.md` — 계정 A 백엔드 API 명세(계약 A의 SSOT — §1 메타데이터 헤더).
- `docs/result-queue-routing.md` — 환경↔SQS 큐 매핑(계정 A Consumer 구현 가이드).

> SSOT 원문은 `gb-backend` 레포 안에 있음. 위 docs는 그 복사본/요약.
