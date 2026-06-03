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
        · development → EC2 HAProxy → WireGuard → 온프렘 MySQL 직접 INSERT

원본 S3 삭제(PII Layer 3)는 Lambda A 책임으로 이관됨(마스킹본 저장 직후 삭제).
B는 페이로드의 masked_text로만 분석하므로 원본을 갖지 않는다.

상세 사양: docs/sb-ai-pipeline-handoff.md §7, docs/ai-pipeline.md §5·§6·§9,
          docs/result-queue-routing.md §3
"""

import json
import logging
import os
from datetime import datetime, timezone

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

# 개발기(development) 경로 — 온프렘 MySQL (EC2 HAProxy → WireGuard 너머)
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
                                "monthly_wage": {"type": ["string", "null"], "description": "십진수 문자열"},
                                "hourly_wage": {"type": ["string", "null"], "description": "십진수 문자열"},
                                "deductions": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "amount": {"type": "string"},
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
        "document_public_id": event.get("document_id"),
        "analysis_document_type": event.get("analysis_document_type") or "UNKNOWN",
        "processing_status": status,
        "overall_risk_level": analysis.get("overall_risk_level"),
        "ocr_confidence": analysis.get("ocr_confidence"),
        "wage_summary": analysis.get("wage_summary"),
        "risk_items": analysis.get("risk_items", []),
        "translated_text": analysis.get("translated_text"),
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
    """development 경로. EC2 프라이빗 IP:3306 → HAProxy → WireGuard → 온프렘 MySQL.

    ⚠️ 온프렘 document_submissions/document_results 스키마는 gb-backend 소관이라
    이 레포에 정의가 없다. 아래 컬럼/SQL은 api-spec §3 결과 필드 기준 best-effort이며,
    배포 전 실제 스키마와 1:1 대조 후 확정해야 한다. (nested는 JSON 컬럼 가정)
    TODO(스키마): submission_id/public_id 키, JSON 컬럼 여부, NOT NULL 제약 확인.
    """
    import pymysql  # 지연 import — production 전용 배포에선 미번들 가능

    conn = pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME, autocommit=False, connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            # 1) 상태 동기화 (PARTIAL은 COMPLETED로 간주 — Consumer 규약과 동일)
            sub_status = "FAILED" if result["processing_status"] == "FAILED" else "COMPLETED"
            cur.execute(
                f"UPDATE {TBL_SUBMISSIONS} SET status=%s WHERE public_id=%s",
                (sub_status, result["document_public_id"]),
            )
            # 2) 결과 UPSERT (멱등 — public_id UNIQUE 가정)
            cur.execute(
                f"""
                INSERT INTO {TBL_RESULTS}
                    (document_public_id, analysis_document_type, processing_status,
                     overall_risk_level, ocr_confidence, wage_summary, risk_items,
                     translated_text, masked_file_url, failed_reason, completed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    processing_status=VALUES(processing_status),
                    overall_risk_level=VALUES(overall_risk_level),
                    ocr_confidence=VALUES(ocr_confidence),
                    wage_summary=VALUES(wage_summary),
                    risk_items=VALUES(risk_items),
                    translated_text=VALUES(translated_text),
                    masked_file_url=VALUES(masked_file_url),
                    failed_reason=VALUES(failed_reason),
                    completed_at=VALUES(completed_at)
                """,
                (
                    result["document_public_id"],
                    result["analysis_document_type"],
                    result["processing_status"],
                    result["overall_risk_level"],
                    result["ocr_confidence"],
                    json.dumps(result["wage_summary"], ensure_ascii=False) if result["wage_summary"] else None,
                    json.dumps(result["risk_items"], ensure_ascii=False),
                    result["translated_text"],
                    result["masked_file_url"],
                    result["failed_reason"],
                    result["completed_at"],
                ),
            )
        conn.commit()
        logger.info("onprem MySQL insert ok: %s", result["document_public_id"])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── 유틸 ──────────────────────────────────────────────────────────────
def _arn_to_queue_url(arn):
    """arn:aws:sqs:{region}:{account}:{name} → https://sqs.{region}.amazonaws.com/{account}/{name}"""
    parts = arn.split(":")
    if len(parts) != 6 or parts[2] != "sqs":
        raise ValueError(f"invalid SQS ARN: {arn}")
    region, account, name = parts[3], parts[4], parts[5]
    return f"https://sqs.{region}.amazonaws.com/{account}/{name}"


def _utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redact(event):
    """로그용 — masked_text 등 큰/민감 필드 제외."""
    return {k: v for k, v in event.items() if k not in ("masked_text",)}
