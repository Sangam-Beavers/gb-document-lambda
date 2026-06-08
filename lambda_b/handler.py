"""
Lambda B — 법령 RAG 분석 + 번역 + 결과 경로 분기 (계정 B AI 분석 파이프라인)

흐름:
  Lambda A의 비동기 invoke 페이로드 수신
    → Bedrock Claude Tool Use 루프 (MCP 패턴)
        · get_legal_standard 툴 → Bedrock Knowledge Bases retrieve (KB 백엔드 = S3 Vectors)
        · submit_analysis 툴 → 모델이 최종 구조화 결과를 제출(강제 스키마)
    → 결과 JSON 조립 (document_results 컬럼 매핑)
    → source로 한 경로 전송 (택일, 동시 전송 아님)
        · production  → result_queue_arn SQS 발행 (body=결과JSON, attr=source/document_public_id)
        · development → VPC 라우트(WireGuard EC2) → 터널 → 온프렘 MySQL 직접 INSERT

원본 S3 삭제(PII Layer 3)는 Lambda A 책임으로 이관됨(마스킹본 저장 직후 삭제).
B는 페이로드의 masked_text로만 분석하므로 원본을 갖지 않는다.

상세 사양: docs/sb-ai-pipeline-handoff.md §7, docs/ai-pipeline.md §5·§6·§9,
          docs/result-queue-routing.md §3
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── 환경 변수 ──────────────────────────────────────────────────────────
REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
# `global.anthropic.claude-sonnet-4-6` 동작 확인됨(2026-06). 텍스트+KB retrieve 경로 검증 완료.
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6")
KB_ID = os.environ.get("LEGAL_KB_ID", "")          # 법령 Knowledge Base ID
KB_NUM_RESULTS = int(os.environ.get("KB_NUM_RESULTS", "5"))
MAX_TOKENS = int(os.environ.get("BEDROCK_MAX_TOKENS", "8000"))
MAX_TOOL_TURNS = int(os.environ.get("MAX_TOOL_TURNS", "8"))  # Tool Use 루프 폭주 방지

# 개발기(development) 경로 — 온프렘 MySQL (WireGuard EC2 터널 너머, HAProxy 미사용)
DB_HOST = os.environ.get("ONPREM_DB_HOST", "")     # 계정 B EC2 프라이빗 IP
DB_PORT = int(os.environ.get("ONPREM_DB_PORT", "3306"))
DB_USER = os.environ.get("ONPREM_DB_USER", "")
DB_PASSWORD = os.environ.get("ONPREM_DB_PASSWORD", "")
DB_NAME = os.environ.get("ONPREM_DB_NAME", "")
TBL_SUBMISSIONS = os.environ.get("ONPREM_TBL_SUBMISSIONS", "document_submissions")
TBL_RESULTS = os.environ.get("ONPREM_TBL_RESULTS", "document_results")

# ── boto3 클라이언트 (핸들러 밖 1회 생성) ──────────────────────────────
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
bedrock_kb = boto3.client("bedrock-agent-runtime", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)
# 원본 S3 삭제(PII Layer 3)는 Lambda A로 이관됨 → B는 s3 클라이언트 불필요.

# ── 분석 프롬프트 ──────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "너는 한국 노동법 기반 근로문서 분석가다. 입력 텍스트는 이미 개인정보가 [항목-마스킹]으로 "
    "가려진 상태다(원본 PII 없음). 절차: (1) 문서를 조항 단위로 분해한다. (2) 위험해 보이는 "
    "조항마다 get_legal_standard 툴로 관련 법령 기준을 조회한다(근로기준법·최저임금법·"
    "외국인근로자고용법 등). (3) 조항을 법령 기준과 비교해 위험 항목과 등급(LOW/MEDIUM/HIGH)을 "
    "산출한다. (4) 급여 정보를 요약한다. (5) 사용자 모국어로 핵심을 번역한다. (6) 모든 분석이 "
    "끝나면 반드시 submit_analysis 툴을 1회 호출해 구조화 결과를 제출한다. "
    "법령 조회가 실패하면 너의 일반 지식으로 보수적으로 판단하고 계속 진행한다."
)

TOOLS = [
    {
        "toolSpec": {
            "name": "get_legal_standard",
            "description": "노동/근로 관련 법령 기준을 유사도 검색으로 조회한다. 조항의 위법 여부 판단에 사용.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "query_text": {
                            "type": "string",
                            "description": "조회할 법적 쟁점/조항 내용 (예: '최저임금 미만 시급', '연장근로 가산수당')",
                        }
                    },
                    "required": ["query_text"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "submit_analysis",
            "description": "분석을 마치면 이 툴로 최종 결과를 제출한다. 정확히 1회만 호출한다.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "processing_status": {"type": "string", "enum": ["COMPLETED", "PARTIAL"]},
                        "overall_risk_level": {"type": ["string", "null"], "enum": ["LOW", "MEDIUM", "HIGH", None]},
                        "ocr_confidence": {"type": ["number", "null"], "description": "추출 신뢰도 0~1, 모르면 null"},
                        "wage_summary": {
                            "type": ["object", "null"],
                            "properties": {
                                "currency_code": {"type": "string", "description": "ISO 4217 (예: KRW)"},
                                "monthly_wage": {"type": ["string", "null"], "description": "순수 십진수 문자열만(예: \"2000000\"). 통화기호·콤마·단위·'약' 금지. 모르면 null"},
                                "hourly_wage": {"type": ["string", "null"], "description": "순수 십진수 문자열만(예: \"9620\"). 통화기호·콤마·단위·'약' 금지. 모르면 null"},
                                "deductions": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "amount": {"type": "string", "description": "순수 십진수 문자열만(예: \"103500\"). 통화기호·콤마·단위·'약' 금지"},
                                        },
                                        "required": ["name", "amount"],
                                    },
                                },
                            },
                            "required": ["currency_code"],
                        },
                        "risk_items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                                    "clause": {"type": "string", "description": "해당 조항·문구"},
                                    "description": {"type": "string", "description": "위험 사유"},
                                },
                                "required": ["risk_level", "clause", "description"],
                            },
                        },
                        "translated_text": {"type": ["string", "null"], "description": "사용자 모국어 번역 전문"},
                        "failed_reason": {"type": ["string", "null"]},
                    },
                    "required": ["processing_status", "risk_items"],
                }
            },
        }
    },
]


def handler(event, context):
    """Lambda A가 비동기 invoke한 페이로드를 받아 분석→전송한다."""
    document_id = event.get("document_id")
    source = event.get("source")
    masked_text = event.get("masked_text") or ""
    logger.info("start: document_id=%s source=%s", document_id, source)

    if not document_id or not source:
        logger.error("missing document_id/source in payload: %s", _redact(event))
        raise ValueError("payload missing document_id/source")

    try:
        analysis = _analyze(
            masked_text=masked_text,
            doc_type=event.get("analysis_document_type") or "UNKNOWN",
            user_lang=event.get("user_lang") or "ko",
        )
    except Exception as e:
        logger.exception("analysis failed: document_id=%s", document_id)
        analysis = {
            "processing_status": "FAILED",
            "risk_items": [],
            "failed_reason": f"analysis error: {type(e).__name__}",
        }

    result = _build_result(event, analysis)
    _dispatch(source, event, result)

    # 원본 S3 삭제는 Lambda A 책임(PII Layer 3, 마스킹본 저장 직후). B는 원본을 갖지 않는다.
    logger.info("done: document_id=%s status=%s", document_id, result["processing_status"])
    return {"document_id": document_id, "processing_status": result["processing_status"]}


# ── 분석 (Tool Use 루프) ───────────────────────────────────────────────
def _analyze(masked_text, doc_type, user_lang):
    """Bedrock Tool Use 루프를 돌려 submit_analysis 입력(구조화 결과)을 반환."""
    user_text = (
        f"[문서유형] {doc_type}\n"
        f"[사용자 모국어] {user_lang}\n"
        f"[마스킹된 문서 본문]\n{masked_text}"
    )
    messages = [{"role": "user", "content": [{"text": user_text}]}]

    for turn in range(MAX_TOOL_TURNS):
        resp = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=messages,
            toolConfig={"tools": TOOLS},
            inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": 0},
        )
        out_msg = resp["output"]["message"]
        messages.append(out_msg)

        tool_uses = [b["toolUse"] for b in out_msg.get("content", []) if "toolUse" in b]
        if not tool_uses:
            # 모델이 툴 없이 종료 → submit_analysis 미호출. 비정상.
            logger.warning("model ended without submit_analysis (turn=%d)", turn)
            raise RuntimeError("model did not call submit_analysis")

        tool_results = []
        for tu in tool_uses:
            if tu["name"] == "submit_analysis":
                logger.info("submit_analysis received (turn=%d)", turn)
                return tu["input"]
            if tu["name"] == "get_legal_standard":
                text = _kb_lookup(tu["input"].get("query_text", ""))
                tool_results.append(
                    {"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"text": text}]}}
                )

        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError(f"tool loop exceeded {MAX_TOOL_TURNS} turns")


def _kb_lookup(query_text):
    """법령 KB retrieve. 실패 시 모델이 자체 지식으로 진행하도록 안내 문자열 반환."""
    if not KB_ID:
        return "법령 KB가 설정되지 않았습니다. 일반 지식으로 보수적으로 판단하세요."
    try:
        res = bedrock_kb.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query_text},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": KB_NUM_RESULTS}},
        )
        chunks = [r["content"]["text"] for r in res.get("retrievalResults", [])]
        if not chunks:
            return "관련 법령을 찾지 못했습니다. 일반 지식으로 판단하세요."
        return "\n---\n".join(chunks)
    except Exception:
        logger.exception("KB retrieve failed: query=%s", query_text[:80])
        return "법령 조회에 실패했습니다. 일반 지식으로 보수적으로 판단하고 계속하세요."


# ── 결과 JSON 조립 (document_results 컬럼 매핑) ─────────────────────────
def _build_result(event, analysis):
    """모델 산출 + 파이프라인 메타를 합쳐 백엔드 계약 JSON으로 만든다."""
    status = analysis.get("processing_status", "COMPLETED")
    return {
        "schema_version": "1.1",  # result-json-schema-agreement.md §2 — Consumer가 검증하는 필수 필드
        "document_public_id": event.get("document_id"),
        "analysis_document_type": event.get("analysis_document_type") or "UNKNOWN",
        "processing_status": status,
        "overall_risk_level": analysis.get("overall_risk_level"),
        "ocr_confidence": analysis.get("ocr_confidence"),
        # 금액은 백엔드 BigDecimal 계약(string decimal, §3-1)에 맞게 정규화한다 — 모델이 종종
        # "약 103,500원"처럼 표시용 문자열을 뱉어 백엔드 역직렬화를 깨뜨린다(2026-06-08 실측).
        "wage_summary": _sanitize_wage_summary(analysis.get("wage_summary")),
        "risk_items": analysis.get("risk_items", []),
        "translated_text": analysis.get("translated_text"),
        # 번역 대상 언어 = user_lang (v1.1 필수 필드, 데모 "ko" 고정).
        "translated_lang": event.get("user_lang") or "ko",
        # masked_file_url은 Lambda A가 만든 s3:// URI. 사용자 노출용 Pre-signed 변환은 백엔드 조회 시점 처리.
        "masked_file_url": event.get("masked_file_url"),
        "failed_reason": analysis.get("failed_reason"),
        "completed_at": _utc_now_iso() if status != "FAILED" else None,
    }


# ── 결과 전송 (source로 택일) ──────────────────────────────────────────
def _dispatch(source, event, result):
    if source == "production":
        _send_to_sqs(event.get("result_queue_arn"), result)
    elif source == "development":
        _insert_onprem_mysql(result)
    else:
        logger.error("unknown source=%s — cannot dispatch", source)
        raise ValueError(f"unknown source: {source}")


def _send_to_sqs(queue_arn, result):
    """production 경로. body=결과JSON(스키마 SSOT), attr=source/document_public_id."""
    if not queue_arn:
        logger.error("production source but result_queue_arn empty")
        raise ValueError("result_queue_arn missing for production source")
    queue_url = _arn_to_queue_url(queue_arn)
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(result, ensure_ascii=False),
        MessageAttributes={
            "source": {"DataType": "String", "StringValue": "production"},
            "document_public_id": {"DataType": "String", "StringValue": result["document_public_id"]},
        },
    )
    logger.info("sent to SQS: %s", queue_url)


def _insert_onprem_mysql(result):
    """development 경로. VPC 라우트(10.10.1.0/24 → WireGuard EC2) → 터널 → 온프렘 MySQL.
    (HAProxy 미사용 — EC2는 WireGuard 터널 엔드포인트일 뿐.)

    연결 (실측 확정 2026-06):
    - **TLS 끔(평문 고정) — `ssl_disabled=True` 필수.** WireGuard 경로에서 TLS 핸드셰이크가
      MTU 문제로 멈춤 → 서버 평문 허용 확인됨. ⚠️ ssl 인자를 "안 주는 것"으로는 부족하다:
      번들 PyMySQL 1.2.0은 인자 미지정 시 PREFERRED 모드라 서버가 SSL을 광고하면(MySQL 8은
      평문 허용이어도 항상 광고) TLS를 시도한다 → ssl_disabled=True로 명시해야 진짜 평문.
      MySQL 8 caching_sha2_password는 PyMySQL이 평문에서도 RSA 공개키 교환으로 자동 인증
      (Connector/J의 allowPublicKeyRetrieval=true와 동일 메커니즘 — 별도 플래그 불필요).

    스키마 (온프렘 document_db 설계 문서와 1:1 대조 확정, 2026-06-04):
    - document_results엔 document_public_id 컬럼이 없다 → `submission_id`(BIGINT NOT NULL
      UNIQUE FK). public_id로 document_submissions.id를 먼저 조회해 넣는다.
    - 마스킹본 컬럼은 `s3_masked_key` — 경로(key)만 저장(s3://버킷 접두사 제거). 조회 시
      백엔드가 presigned URL 생성.
    - **`analysis_document_type`은 results에도 있다(v1.1 신규, NOT NULL — 2026-06-07 수정).**
      이전 전제("submissions 소유라 results엔 없음")는 v1.0 기준 오류 — 백엔드 DocumentResult
      엔티티가 NOT NULL enum으로 매핑하고 GET /result 응답 변환에서 `.name()`을 바로 호출하므로,
      이 컬럼 없이 INSERT하면 적재는 돼도 조회가 NPE → COMMON5000(500)으로 터진다(실측).
      값은 페이로드 대신 **document_submissions의 동일 컬럼에서 가져온다**(백엔드가 제출 시점에
      검증해 넣은 정본 — 페이로드의 "UNKNOWN" 폴백이 백엔드 enum 변환을 깨는 것 방지).
      `translated_lang`(v1.1 신규, NULL 허용)도 함께 적재한다.
    - completed_at은 DATETIME NOT NULL — ISO 'T'/'Z' 제거 변환, FAILED(None)면 현재 시각 대체.
    - **document_submissions.status도 함께 갱신한다** (2026-06-05 수정). 백엔드 GET /status
      폴링은 submissions.status(ANALYZING/COMPLETED/FAILED)만 읽으므로, results만 INSERT하면
      프론트가 영원히 "분석 중"에 머문다(실측). 운영 경로의 SQS Consumer
      (gb-backend AnalysisResultIngestServiceImpl §4)와 동일 매핑: FAILED→FAILED,
      COMPLETED/PARTIAL→COMPLETED. 같은 트랜잭션으로 원자 커밋.
      (이전 docstring의 "UPLOADED/SENT_TO_AWS/FAILED_UPLOAD 전용" 전제는 백엔드 실제
      enum과 불일치한 오류였음 — 그런 상태값은 백엔드에 존재하지 않는다.)
    """
    import pymysql  # 지연 import — production 전용 배포에선 미번들 가능

    # completed_at NOT NULL — FAILED(None)여도 기록 시각으로 채운다.
    completed_at = (result["completed_at"] or _utc_now_iso()).replace("T", " ").rstrip("Z")
    failed_reason = result["failed_reason"]
    if failed_reason:
        failed_reason = failed_reason[:255]  # VARCHAR(255)

    conn = pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME, charset="utf8mb4", autocommit=False,
        connect_timeout=10, read_timeout=15, write_timeout=15,  # 무응답 행 방지 — 빠른 에러
        ssl_disabled=True,  # ⚠️ 필수 — PyMySQL 1.2.0은 미지정 시 PREFERRED(TLS 시도). docstring 참고.
    )
    try:
        with conn.cursor() as cur:
            # 1) public_id → submissions.id + analysis_document_type
            #    (results.submission_id FK NOT NULL / analysis_document_type NOT NULL — 정본은 submissions)
            cur.execute(
                f"SELECT id, analysis_document_type FROM {TBL_SUBMISSIONS} WHERE public_id=%s",
                (result["document_public_id"],),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(
                    f"submission not found in onprem DB: public_id={result['document_public_id']}"
                )
            submission_id, analysis_document_type = row[0], row[1]

            # 2) 결과 UPSERT (submission_id UNIQUE로 멱등).
            #    created_at/updated_at은 UTC_TIMESTAMP(6) — 백엔드(JPA Auditing)가 UTC로 쓰고
            #    응답 직렬화 시 UTC로 간주해 'Z'를 붙이므로, 서버 타임존 의존 NOW(6) 대신 UTC 명시.
            cur.execute(
                f"""
                INSERT INTO {TBL_RESULTS}
                    (submission_id, analysis_document_type, processing_status, overall_risk_level,
                     ocr_confidence, wage_summary, risk_items, translated_text, translated_lang,
                     s3_masked_key, failed_reason, completed_at, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))
                ON DUPLICATE KEY UPDATE
                    analysis_document_type=VALUES(analysis_document_type),
                    processing_status=VALUES(processing_status),
                    overall_risk_level=VALUES(overall_risk_level),
                    ocr_confidence=VALUES(ocr_confidence),
                    wage_summary=VALUES(wage_summary),
                    risk_items=VALUES(risk_items),
                    translated_text=VALUES(translated_text),
                    translated_lang=VALUES(translated_lang),
                    s3_masked_key=VALUES(s3_masked_key),
                    failed_reason=VALUES(failed_reason),
                    completed_at=VALUES(completed_at),
                    updated_at=UTC_TIMESTAMP(6)
                """,
                (
                    submission_id,
                    analysis_document_type,
                    result["processing_status"],
                    result["overall_risk_level"],
                    result["ocr_confidence"],
                    json.dumps(result["wage_summary"], ensure_ascii=False) if result["wage_summary"] else None,
                    json.dumps(result["risk_items"], ensure_ascii=False),
                    result["translated_text"],
                    result.get("translated_lang"),
                    _s3_uri_to_key(result["masked_file_url"]),
                    failed_reason,
                    completed_at,
                ),
            )

            # 3) submissions.status 동기화 — 운영 SQS Consumer와 동일 매핑(docstring 참고).
            #    이게 없으면 백엔드 GET /status가 ANALYZING으로 남아 프론트 폴링이 끝나지 않는다.
            submission_status = (
                "FAILED" if result["processing_status"] == "FAILED" else "COMPLETED"
            )
            cur.execute(
                f"UPDATE {TBL_SUBMISSIONS} SET status=%s, updated_at=UTC_TIMESTAMP(6) WHERE id=%s",
                (submission_status, submission_id),
            )
        conn.commit()
        logger.info("onprem MySQL insert ok: %s (submission_id=%s)",
                    result["document_public_id"], submission_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── 금액 정규화 (백엔드 BigDecimal 계약 보호) ──────────────────────────
_NON_NUMERIC = re.compile(r"[^0-9.\-]")


def _normalize_amount(value):
    """금액을 깨끗한 decimal 문자열로 정규화. 통화기호·콤마·공백·한글 등 비숫자 제거.

    백엔드 document_results의 wage_summary는 BigDecimal 필드로 매핑되며, 스키마 합의(§3-1)는
    monthly_wage/hourly_wage/deductions[].amount를 'string(decimal)'로 규정한다. 모델이
    "약 103,500원" 같은 표시용 문자열을 넣으면 백엔드 역직렬화가 깨져 그 행이 섞인 조회 전체가
    500(개발 읽기)/DLQ(운영 SQS 수신)로 떨어진다. 파싱 불가/빈 값은 None('정보 없음')으로 둔다.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool은 int 하위형 — 금액 아님, 방어.
        return None
    if isinstance(value, (int, float)):
        return str(value)
    cleaned = _NON_NUMERIC.sub("", str(value))
    if cleaned in ("", "-", ".", "-.", "."):
        return None
    try:
        return str(Decimal(cleaned))  # 중복 소수점 등은 InvalidOperation으로 걸러진다.
    except (InvalidOperation, ValueError):
        return None


def _sanitize_wage_summary(wage):
    """wage_summary 내 모든 금액 필드를 _normalize_amount로 정규화한다(없으면 그대로 반환)."""
    if not wage:
        return wage
    wage["monthly_wage"] = _normalize_amount(wage.get("monthly_wage"))
    wage["hourly_wage"] = _normalize_amount(wage.get("hourly_wage"))
    for deduction in wage.get("deductions") or []:
        deduction["amount"] = _normalize_amount(deduction.get("amount"))
    return wage


# ── 유틸 ──────────────────────────────────────────────────────────────
def _arn_to_queue_url(arn):
    """arn:aws:sqs:{region}:{account}:{name} → https://sqs.{region}.amazonaws.com/{account}/{name}"""
    parts = arn.split(":")
    if len(parts) != 6 or parts[2] != "sqs":
        raise ValueError(f"invalid SQS ARN: {arn}")
    region, account, name = parts[3], parts[4], parts[5]
    return f"https://sqs.{region}.amazonaws.com/{account}/{name}"


def _s3_uri_to_key(uri):
    """'s3://bucket/masked/a.txt' → 'masked/a.txt'. 온프렘 s3_masked_key엔 경로(key)만 저장."""
    if not uri:
        return None
    if uri.startswith("s3://"):
        rest = uri[5:]
        return rest.split("/", 1)[1] if "/" in rest else rest
    return uri


def _utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redact(event):
    """로그용 — masked_text 등 큰/민감 필드 제외."""
    return {k: v for k, v in event.items() if k not in ("masked_text",)}
