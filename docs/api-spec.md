# AI 서류 분석 — API 명세 (정본)

> Notion 개별 상세 명세 기반 정본. 전역 규칙은 [`../conventions.md`](../conventions.md).
> 성공 응답은 `{ "success": true, "data": {...}, "message": "..." }` 래퍼. 아래 표 필드는 `data` 내부.
> 서류 유형 필드는 **`analysis_document_type`** (신분증의 `identity_document_type`과 구분).

---

## 엔드포인트 목록

| API | Method | Endpoint | Auth |
| --- | --- | --- | --- |
| 서류 분석 내역 목록 | GET | `/api/v1/documents` | ✅ |
| 분석 요청 (Pre-signed URL 발급) | POST | `/api/v1/documents` | ✅ |
| 분석 진행 상태 조회 (폴링) | GET | `/api/v1/documents/{id}/status` | ✅ |
| 분석 결과 상세 조회 | GET | `/api/v1/documents/{id}/result` | ✅ |
| 분석 재요청 (FAILED 시) | POST | `/api/v1/documents/{id}/retry` | ✅ |
| 문서 분석 결과 단건 조회 | GET | `/api/v1/documents/{id}` | ✅ |

---

## 1. 분석 요청 (Pre-signed URL 발급)

`POST /api/v1/documents` · Auth ✅

문서 레코드를 생성하고 S3 Pre-signed URL을 발급한다. 클라이언트는 응답받은 URL로 파일을 직접 PUT 업로드한다.

**Request Body**
| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `analysis_document_type` | string | O | LABOR_CONTRACT / PAYSLIP / EMPLOYMENT_CONTRACT |
| `file_name` | string | O | 원본 파일명(확장자 포함, 예: `contract.pdf`) |

**Response 201** — `data`
| 필드 | 타입 | nullable | 설명 |
| --- | --- | --- | --- |
| `public_id` | string | N | 문서 UUID. 이후 상태·결과 조회 키 |
| `upload_url` | string | N | S3 Pre-signed URL. 클라이언트가 PUT으로 직접 업로드 |
| `upload_headers` | object(map) | N | **PUT 업로드 시 그대로 함께 보내야 하는 헤더(이름→값).** 서명에 포함되어 있어 누락/변경 시 S3가 403으로 거부하고 메타데이터(source/document_id/result_queue_arn)가 오브젝트에 박히지 않는다. 예: `{"Content-Type":"application/octet-stream","x-amz-meta-source":"production","x-amz-meta-document_id":"…"}` |
| `expires_at` | string | N | URL 만료 시각 (ISO 8601 UTC Z) |

> ⚠️ **업로드 시 `upload_headers`를 반드시 그대로 전송**해야 한다. AWS SDK v2 presigner는 S3 오브젝트
> 메타데이터를 서명 헤더(`X-Amz-SignedHeaders`)에 굽기 때문에, 이 헤더들을 이름·값 그대로 PUT에 실어야
> 서명이 일치한다. 백엔드가 정한 메타데이터 값을 클라이언트가 임의로 바꾸면 안 된다(서명 불일치 → 403).
> `Host`는 HTTP 클라이언트가 자동 설정하므로 `upload_headers`에 포함하지 않는다.
>
> 업로드 예시:
> ```
> PUT {upload_url}
> Content-Type: application/octet-stream
> x-amz-meta-source: production
> x-amz-meta-document_id: 550e8400-…
> x-amz-meta-result_queue_arn: arn:aws:sqs:…   # production 계열에서만 존재
> <binary file body>
> ```

**Error**
| HTTP | code | message |
| --- | --- | --- |
| 400 | COMMON4001 | 요청 값이 올바르지 않습니다. |
| 400 | COMMON4002 | 필수 입력 항목이 누락되었습니다. |
| 401 | COMMON4011 | 인증 정보가 유효하지 않습니다. |

---

## 2. 분석 진행 상태 조회 (폴링)

`GET /api/v1/documents/{id}/status` · Auth ✅

**Path Variable**: `id` = 문서 public_id (UUID)

**Response 200** — `data`
| 필드 | 타입 | nullable | 설명 |
| --- | --- | --- | --- |
| `public_id` | string | N | 문서 UUID |
| `status` | string | N | ANALYZING / COMPLETED / FAILED |
| `estimated_minutes` | integer | Y | 예상 소요(분). 진행 중일 때 안내용 |

**Error**: 401 COMMON4011 / 403 COMMON4031 / 404 DOCUMENT4001

---

## 3. 분석 결과 상세 조회

`GET /api/v1/documents/{id}/result` · Auth ✅

분석 완료 문서의 상세 결과를 조회한다.

**Path Variable**: `id` = 문서 public_id (UUID)

**Response 200** — `data`
| 필드 | 타입 | nullable | 설명 |
| --- | --- | --- | --- |
| `document_public_id` | string | N | 문서 UUID |
| `analysis_document_type` | string | N | LABOR_CONTRACT / PAYSLIP / EMPLOYMENT_CONTRACT |
| `processing_status` | string | N | COMPLETED / FAILED / PARTIAL |
| `overall_risk_level` | string | Y | LOW / MEDIUM / HIGH. 실패 시 null |
| `ocr_confidence` | number | Y | OCR 신뢰도 (0~1, 표시용 number). 실패 시 null |
| `wage_summary` | object | Y | 급여 요약. 없으면 null |
| `wage_summary.currency_code` | string | N | 통화 (ISO 4217) |
| `wage_summary.monthly_wage` | string | Y | 월 급여 (string 십진수) |
| `wage_summary.hourly_wage` | string | Y | 시급 (string 십진수) |
| `wage_summary.deductions` | array | Y | 공제 항목. 없으면 [] |
| `wage_summary.deductions[].name` | string | N | 공제 항목명 |
| `wage_summary.deductions[].amount` | string | N | 공제 금액 (string 십진수) |
| `risk_items` | array | Y | 위험 항목. 없으면 [] |
| `risk_items[].risk_level` | string | N | LOW / MEDIUM / HIGH |
| `risk_items[].clause` | string | N | 해당 조항·문구 |
| `risk_items[].description` | string | N | 위험 사유 |
| `translated_text` | string | Y | 번역 전문. 미생성 시 null |
| `masked_file_url` | string | Y | 마스킹본 Pre-signed URL. 미생성 시 null |
| `failed_reason` | string | Y | 실패 사유 (FAILED/PARTIAL일 때) |
| `completed_at` | string | Y | 완료 시각 (ISO 8601 UTC Z). 미완료 시 null |
| `created_at` | string | N | 결과 생성 시각 |
| `updated_at` | string | N | 결과 갱신 시각 |

**Error**
| HTTP | code | message |
| --- | --- | --- |
| 401 | COMMON4011 | 인증 정보가 유효하지 않습니다. |
| 403 | COMMON4031 | 접근 권한이 없습니다. |
| 404 | DOCUMENT4001 | 존재하지 않는 문서입니다. |
| 422 | COMMON4221 | 처리할 수 없는 요청입니다. (분석 미완료 상태 조회 등) |

> 결과 화면에는 면책 문구를 항상 함께 노출한다(요구사항 §3 참고).

---

## 4. 서류 분석 내역 목록

`GET /api/v1/documents?page=&size=` · Auth ✅

**Response 200** — `data`: `documents`(배열) + 페이지네이션 메타.
각 항목: `public_id`, `analysis_document_type`, `status`, `overall_risk_level`, `created_at` 등 요약.

> 첫 화면 최근 3건 노출, 추가(최대 10건)는 월 구독(15,000원). 구독 게이팅은 비즈니스 정책으로 처리.

---

## 5. 분석 재요청

`POST /api/v1/documents/{id}/retry` · Auth ✅

FAILED 상태 문서의 분석을 다시 트리거. (S3 원본 유지 시 재사용, 아니면 재업로드 URL 재발급)

**Error**: 401 COMMON4011 / 403 COMMON4031 / 404 DOCUMENT4001 / 422 COMMON4221

---

## 6. 문서 분석 결과 단건 조회

`GET /api/v1/documents/{id}` · Auth ✅ — 마이페이지 진입용 단건 조회. 응답은 §3의 결과 또는 메타 요약(구현 시 통일).

---

## 참고

계정 B 내부 분석 파이프라인(Lambda/Bedrock/S3 Vectors)은 [`ai-pipeline.md`](./ai-pipeline.md) 참고. 백엔드(계정 A) 관점에서는 위 API만 구현하면 되고, 결과는 SQS Consumer가 `document_results`에 채운다.
