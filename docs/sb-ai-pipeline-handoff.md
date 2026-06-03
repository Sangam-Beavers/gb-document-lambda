# sb-ai-pipeline 레포 핸드오프 (계정 B · Lambda A/B 전용)

> 이 문서는 `gb-backend`(계정 A · Java/Spring) 작업 세션에서 결정된 내용을 새 레포로 옮기기 위한 요약본이다.
> 새 레포(`sb-ai-pipeline` 가칭)에 그대로 복사해 `README.md` 또는 `docs/handoff.md`로 두면 된다.
> **이 레포 범위: Lambda A + Lambda B만.** 챗봇 Lambda · MCP Server는 별도 레포에서 진행.
> SSOT: `gb-backend/docs/document-analysis/ai-pipeline.md`, `architecture.md`, `CLAUDE.md`.

---

## 1. 이 레포의 범위 (무엇을 만드나)

**계정 B(AI 격리 계정)의 분석 파이프라인 Lambda 2개.**

| 구성요소 | 역할 | 트리거 |
| --- | --- | --- |
| **Lambda A** | 텍스트 추출 + PII 마스킹 (Bedrock VLM 1회) | S3 ObjectCreated |
| **Lambda B** | RAG 분석 + 번역 + 결과 경로 분기 | Lambda A의 비동기 invoke |
| (선택) **infra/** | Terraform/CDK — Lambda·SQS·KB·VPC 엔드포인트 | — |

**이 레포에 없는 것 (다른 레포 소관):**
- **챗봇 Lambda · MCP Server** → 별도 레포에서 진행
- 계정 A · gb-backend 소관:
  - `POST /api/v1/documents` (Pre-signed URL + S3 메타데이터 심기)
  - SQS Consumer (`gb-analysis-results-{stage|prod}`)
  - 조회 API (`/status`, `/result`, 목록)

---

## 2. 디렉터리 구조 (제안)

```
sb-ai-pipeline/
├── README.md
├── docs/
│   └── handoff.md                # 이 문서
├── lambda_a/                     # 추출 + 마스킹
│   ├── handler.py
│   └── requirements.txt          # 비어있거나 boto3만
├── lambda_b/                     # RAG 분석 + 경로 분기
│   ├── handler.py
│   └── requirements.txt
└── infra/                        # (선택) IaC
```

> Lambda A/B는 같은 레포에 두는 게 맞다 — 계정/언어/스택이 동일(계정 B, Python, boto3) + Lambda A가 Lambda B를 직접 invoke하므로 페이로드 계약이 한 레포 안에서 닫힘.

---

## 3. 배포 방식 결정: **ZIP** (컨테이너 이미지 아님)

세션에서 확정:
- 이미지/PDF 전처리 라이브러리(`poppler`, `pdf2image` 등) **안 씀**.
- 원본 바이트를 Bedrock(Claude Vision)에 직접 넘겨 1회 호출로 추출+마스킹 동시 처리.
- 의존성은 사실상 `boto3` 하나 → Lambda Python 런타임 내장.
- 따라서 컨테이너 이미지(ECR) 불필요. **ZIP 배포로 충분.**

**부수 효과 (인프라 정정):** 원본 문서 `ai-pipeline.md` §2의 `ECR VPC Endpoint`는 이 결정에 따라 **제거 대상**. 새 레포 README에 명시하고 인프라 코드(IaC)에서도 제거.

런타임/설정:
```
런타임: python3.12
타임아웃: Lambda A = 300s, Lambda B = 600s
메모리: 처음엔 512MB~1024MB로 시작, 측정 후 조정
VPC: 계정 B 관리 서브넷 (sb-mgmt-subnet-a/c)
필요 VPC Endpoint: Bedrock, S3 Gateway, SQS
```

---

## 4. Bedrock 모델 ID

**기준 모델: `anthropic.claude-sonnet-4-6` (Bedrock, Vision + Tool Use 모두 지원)**

- Lambda A (Vision/VLM 추출+마스킹) · Lambda B (Tool Use RAG 루프) **모두 같은 모델 사용**.

### ⚠️ 서울 리전(`ap-northeast-2`) 호출 시 주의 (R3)

서울 리전에서는 **foundation model ID 직접 호출이 막혀 있어** inference profile로만 호출해야 한다. 따라서 실제 코드에서 `modelId`에 박을 값은 둘 중 하나:

```
apac.anthropic.claude-sonnet-4-6
global.anthropic.claude-sonnet-4-6
```

- **`us.` 접두사 금지** → `400 invalid model identifier`.
- Day 1에 실제로 보이는 정확한 ID를 확인:
  ```bash
  aws bedrock list-inference-profiles --region ap-northeast-2
  ```
- 위 명령으로 나오는 ID 중 `*claude-sonnet-4-6*`을 그대로 사용 (추측 금지).
- IAM에는 inference-profile ARN + 라우팅 대상 리전들의 `foundation-model` 리소스도 함께 Allow.

---

## 5. Lambda A 사양 (이번에 만들 것)

### 트리거
- **S3 ObjectCreated** (계정 B의 업로드 버킷)
- 클라이언트가 백엔드에서 받은 Pre-signed URL로 직접 PUT한 직후 발화

### 입력
- S3 오브젝트 본체 (이미지 또는 PDF)
- **오브젝트 메타데이터** (계정 A 백엔드가 Pre-signed URL의 서명 헤더로 심음):
  - `source` — `production` | `development`
  - `document_id` — 분석 식별자
  - `result_queue_arn` — `source=production`일 때만 (stage/prod 환경별 SQS ARN)
  - `user_lang` — 답변 번역 언어 (Lambda B가 사용, A는 그냥 전달)

> ⚠️ `head_object` 응답의 `Metadata` 딕셔너리에서는 `x-amz-meta-` 접두사가 빠진 채로 나온다 (`source`, `document_id` 등). 백엔드의 키 이름과 1:1로 맞출 것.

### 처리 (handler.py가 하는 일)
1. `event["Records"]`에서 bucket/key 파싱
2. `s3.head_object`로 메타데이터 읽기 (source, document_id, result_queue_arn, user_lang)
3. `s3.get_object`로 원본 바이트 로드
4. **Bedrock Claude Vision 1회 호출** (Converse API, modelId = `apac.anthropic.claude-sonnet-4-6`)
   - 입력: 원본 이미지(base64) 또는 PDF(document 블록)
   - 프롬프트: "텍스트 추출 + PII 마스킹 동시 수행, `[항목-마스킹]` 형식, 표 구조·조항 번호 보존"
   - 마스킹 대상: 이름·주민등록번호·외국인등록번호·전화번호·주소·계좌번호
5. **원본 바이트 변수 즉시 해제** (`del raw_bytes`) — 메모리 잔존 최소화
6. 마스킹된 텍스트를 별도 S3 키(`masked/`)에 업로드 → `masked_file_url` 확보
7. **`original/` S3 원본 삭제** (PII Layer 3) — 마스킹본 저장 성공 직후. B는 `masked_text`로만 분석하므로 원본 불필요. best-effort(실패해도 7일 수명주기 백스톱).
8. **Lambda B 비동기 호출** (`lambda.invoke(InvocationType="Event")`)
   - 페이로드: `{s3_key, masked_text, masked_file_url, document_id, user_lang, source, result_queue_arn}`
9. 즉시 종료 (Lambda B 완료를 기다리지 않음 — 비동기)

### 출력
- Lambda B로 위 페이로드를 비동기 발사한 직후 종료.
- 실패 시 (VLM 오류 등) → Lambda B 미호출, **해당 환경 DB에 FAILED 기록**해야 하는데 이건 source에 따라 경로가 다름. 데모 단계에서는 CloudWatch 로그 + S3 원본 유지(7일 수명주기)로 단순화 가능.

### IAM 권한 (Lambda A 실행 역할)
```
s3:GetObject, s3:PutObject  (업로드 버킷)
s3:DeleteObject             (업로드 버킷 — PII Layer 3 원본 삭제. B에서 A로 이관)
s3:GetObjectMetadata        (head_object용 — GetObject에 포함)
bedrock:InvokeModel         (Sonnet 4.6 inference profile + 라우팅 대상 foundation model)
lambda:InvokeFunction       (Lambda B만)
logs:*                      (CloudWatch Logs 기본)
```

---

## 6. 계정 A 백엔드(gb-backend)와의 계약 (Contract)

이게 양 레포의 인터페이스라 가장 중요. 한쪽이 바꾸면 다른 쪽도 같이 바꿔야 함.

### A. S3 오브젝트 메타데이터 (백엔드 → Lambda A 입구)
백엔드가 Pre-signed URL의 서명 헤더(`X-Amz-SignedHeaders`)에 포함해 클라이언트가 PUT 시 전송. Lambda A가 `head_object`로 읽음.

| 메타키 (S3 wire) | head_object Metadata 키 | 값 | 비고 |
| --- | --- | --- | --- |
| `x-amz-meta-source` | `source` | `production` \| `development` | 인프라 계열 결정 |
| `x-amz-meta-document-id` | `document-id` 또는 `document_id` | UUID | DB row 식별 |
| `x-amz-meta-result-queue-arn` | `result-queue-arn` | SQS ARN | `source=production`일 때만. stage/prod 큐 ARN |
| `x-amz-meta-user-lang` | `user-lang` | `ko` 등 | Lambda B 번역 언어 |

> S3는 메타키 하이픈/언더스코어 정규화에 약간 까다롭다. 백엔드 구현과 Lambda 파싱 양쪽에서 **실제 PUT된 키 이름을 로그로 확인**하고 통일.

### B. Lambda A → Lambda B 페이로드 (비동기 invoke)
```json
{
  "document_id": "uuid",
  "s3_key": "uploads/...",
  "masked_text": "...",
  "masked_file_url": "s3://.../masked/...",
  "user_lang": "ko",
  "source": "production",
  "result_queue_arn": "arn:aws:sqs:ap-northeast-2:...:gb-analysis-results-prod"
}
```

### C. Lambda B → 결과 전달 경로 (source로 분기)
- `source=production` → **`result_queue_arn` 큐로 발행** (백엔드가 심은 ARN을 그대로 사용 → Lambda는 환경 매핑 테이블 몰라도 됨)
  - 큐는 `gb-analysis-results-stage` / `gb-analysis-results-prod`로 **물리 분리**
  - 각 환경 백엔드 SQS Consumer가 자기 큐만 구독 → 자기 Aurora에 저장
- `source=development` → 계정 B EC2 (HAProxy) → WireGuard 터널 → 온프렘 MySQL 직접 INSERT

---

## 7. Lambda B 사양 (다음 작업)

- **모델:** `apac.anthropic.claude-sonnet-4-6` (Vision 불필요하지만 통일, Tool Use 지원)
- **RAG:** Bedrock Knowledge Bases `retrieve` (KB 백엔드 = S3 Vectors)
  ```python
  bedrock_kb = boto3.client("bedrock-agent-runtime", region_name="ap-northeast-2")
  res = bedrock_kb.retrieve(knowledgeBaseId="<LEGAL_KB_ID>", retrievalQuery={"text": q}, ...)
  ```
- **타임아웃:** 600s
- **처리:** 조항 분해 → Tool 호출(`get_legal_standard`) 시 KB `retrieve` 호출 → 조항↔법령 비교로 위험 항목·등급 산출 → 급여 요약 → 모국어 번역 → 결과 JSON 조립 → `source`에 따라 한 경로로 전송
- **출력 JSON 컬럼 매핑 (백엔드 `document_results` 테이블에 들어감):**
  ```
  processing_status, overall_risk_level, ocr_confidence,
  wage_summary{currency_code, monthly_wage, hourly_wage, deductions[]},
  risk_items[]{risk_level, clause, description},
  translated_text, masked_file_url, failed_reason, completed_at
  ```
- **S3 원본 삭제는 Lambda A가 이미 수행**(PII Layer 3, 마스킹본 저장 직후). B는 원본을 보유하지 않으며 `masked_text`로만 분석한다.

### IAM 권한 (Lambda B 실행 역할)
```
bedrock:InvokeModel            (Sonnet 4.6 inference profile)
bedrock:Retrieve               (법령 KB — bedrock-agent-runtime)
sqs:SendMessage                (계정 A의 result_queue_arn — 크로스 계정 정책 필요)
ec2 네트워크 경로              (development 경로일 때 EC2 HAProxy → WireGuard → 온프렘 MySQL)
logs:*
```

---

## 8. 계정 B 인프라 메모

### VPC: `sb-ai-vpc` (10.110.0.0/16)
- 퍼블릭 서브넷 (2 AZ): `sb-public-subnet-a/c` — WireGuard EC2 + HAProxy + EIP
- 관리(프라이빗) 서브넷 (2 AZ): `sb-mgmt-subnet-a/c` — Lambda + VPC Endpoints
- DB 서브넷 **없음** (Aurora 제거, 법령 = S3 Vectors)

### VPC Endpoints (관리 서브넷에서 사용)
- Bedrock (`bedrock-runtime`, `bedrock-agent-runtime`)
- S3 (Gateway)
- SQS (크로스 계정으로 계정 A의 큐 발행 — IAM 정책으로 허용)
- ~~ECR~~ (ZIP 배포 결정으로 제거)

### 계정 B 격리 데이터 (이 레포가 다루는 부분)
- **법령 KB** (Bedrock Knowledge Bases, 백엔드 = S3 Vectors)
  - 분석 파이프라인이 사용 (챗봇 레포도 같은 KB 공유 — 별도 레포지만 같은 KB 인스턴스 1개)
  - 데이터: 공공누리 1유형 법령 (근로기준법·최저임금법·외국인근로자고용법 등) S3 적재 → KB 동기화
- **업로드 S3 버킷** + **마스킹본 S3 키 prefix** (또는 별도 버킷)

> DynamoDB · 챗봇 Redis는 이 레포 무관(챗봇 레포 소관).

---

## 9. PII 3-Layer 보호 (반드시 지킬 것)

| Layer | 조치 | Lambda 측 책임 |
| --- | --- | --- |
| 1. 수집 최소화 | 클라이언트 → S3 직접 업로드 (백엔드 미경유) | 백엔드 책임, 이 레포 무관 |
| 2. 처리 중 마스킹 | Claude VLM 1회로 추출+마스킹, **원본 메모리 즉시 소멸** | **Lambda A 책임** — 원본 바이트 변수 `del`, 함수 스코프 밖으로 새지 않게 |
| 3. 사후 삭제 | 마스킹본 S3 저장 직후 `original/` 원본 삭제 | **Lambda A 책임** — B는 `masked_text`만 쓰므로 원본 불필요, 최대한 일찍 삭제 |

근거: 개인정보보호법 제16조(최소 수집).

---

## 10. Day 1 PoC 순서 (권장)

1. `aws bedrock list-inference-profiles --region ap-northeast-2` — Sonnet 4.6의 정확한 inference profile ID 확정 (apac. 또는 global.)
2. 빈 Lambda 함수 1개 ZIP 배포해서 기본 동작 확인 (`hello world` 리턴)
3. S3 ObjectCreated 트리거 연결 확인 (테스트 파일 PUT → CloudWatch 로그 확인)
4. Bedrock Vision Converse API 호출 PoC (작은 이미지 1장 → 텍스트 추출 확인)
5. 위 둘 합쳐 Lambda A 1차 구현 → Lambda B는 더미로 두고 invoke 발사 확인
6. (병행) 백엔드와 메타데이터 키 이름 1:1 매칭 확정 + 로그로 검증

---

## 11. 새 레포에 들고 갈 참고 문서 (gb-backend에서 복사 가능)

원문 SSOT는 `gb-backend` 안에 있으니, 새 레포에서 참고하려면 다음을 복사하거나 링크:

| 파일 | 용도 |
| --- | --- |
| `docs/document-analysis/ai-pipeline.md` | 분석 파이프라인 정본 (Lambda A/B 명세 원본) |
| `docs/architecture.md` | 계정 A/B 분리, VPC 구조 |
| `docs/tech-stack.md` | 기술 스택 + 데이터 저장 정책 |
| `docs/document-analysis/api-spec.md` §1 | Pre-signed URL의 메타데이터 헤더 사양 (계약 A의 SSOT) |
| `docs/document-analysis/result-queue-routing.md` | 환경↔큐 매핑 |

> 챗봇/MCP 관련(`ai-chatbot-mcp.md`)은 이 레포 범위 밖이라 복사 불필요.

---

## 12. 정정 필요한 기존 문서 (gb-backend 측)

이 세션의 결정으로 원문이 살짝 어긋난 부분 — 별도 커밋으로 반영하면 좋음:

1. **`ai-pipeline.md` §2** — `ECR VPC Endpoint ← Lambda 컨테이너 이미지 pull` 줄 제거 (ZIP 배포 결정).
2. **`ai-pipeline.md` §4** — Lambda A → B 페이로드에 **`result_queue_arn` 추가** (현재 `{s3_key, masked_text, document_id, user_lang, source}`로만 적혀 있음. Lambda B가 production 경로에서 ARN으로 발행하려면 A가 relay해야 함).

---

*Handoff prepared for sb-ai-pipeline | 계정 B Lambda A/B 신규 레포 | 2026-05-31*
