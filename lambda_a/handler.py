"""
Lambda A — 텍스트 추출 + PII 마스킹 (계정 B AI 분석 파이프라인)

흐름:
  S3 ObjectCreated 트리거
    → head_object로 메타데이터 읽기 (source, document_id, result_queue_arn, user_lang)
    → get_object로 원본 바이트 로드
    → Bedrock Claude Vision 1회 호출 (추출 + PII 마스킹 동시)
    → 원본 바이트 변수 즉시 del (PII Layer 2 — 메모리 잔존 최소화)
    → 마스킹 텍스트를 masked/ 키에 업로드
    → original/ S3 원본 삭제 (PII Layer 3 — 마스킹본 저장 직후)
    → Lambda B 비동기 invoke (InvocationType="Event") 후 즉시 종료

PII 삭제 책임:
  Lambda B는 페이로드의 masked_text(+masked_file_url)로만 RAG 분석하므로 원본이
  더 이상 필요 없다. 따라서 마스킹본 저장이 성공한 직후 A가 원본을 삭제한다
  (메모리 Layer 2 + S3 Layer 3 모두 A 책임). 마스킹본을 먼저 영속화한 뒤
  원본을 지우므로 어떤 경우에도 데이터가 마스킹본 없이 사라지지 않는다.

실패 정책(데모 단계, 핸드오프 §5):
  Lambda A는 SQS/DB 권한이 없다(IAM: s3 get/put/delete + bedrock/lambda/logs).
  추출 실패·메타 누락 시 → 구조화 로그 + 예외 전파(이 경우 원본 삭제 전이므로 원본 유지 →
  7일 수명주기 삭제). 마스킹 성공 후엔 원본을 삭제하므로, 이후 B 실패의 재시도는
  원본이 아니라 masked_text/masked_file_url로 한다. 운영 전환 시 source 경로별
  FAILED 기록(production→큐 / development→온프렘)을 별도 도입한다.

상세 사양: docs/sb-ai-pipeline-handoff.md §5·§6, docs/ai-pipeline.md §4·§9
"""

import json
import logging
import os
import re
from urllib.parse import unquote_plus

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── 환경 변수 (Lambda 콘솔/IaC에서 주입) ────────────────────────────────
REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
# 서울 리전은 foundation model 직접 호출 불가 → inference profile ID 필수.
# `global.anthropic.claude-sonnet-4-6` 동작 확인됨(2026-06, Bedrock 콘솔). `us.` 접두사 금지.
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6")
LAMBDA_B_NAME = os.environ.get("LAMBDA_B_NAME", "")  # 비동기 invoke 대상
MASKED_PREFIX = os.environ.get("MASKED_PREFIX", "masked/")
MAX_TOKENS = int(os.environ.get("BEDROCK_MAX_TOKENS", "8000"))

# ── boto3 클라이언트 (핸들러 밖에서 1회 생성 → 콜드스타트 외 재사용) ──────
s3 = boto3.client("s3", region_name=REGION)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)

# ── 분류 + 추출 + 마스킹 프롬프트 ───────────────────────────────────────
# 한 번의 Vision 호출로 (1) 문서유형 판정 (2) 본문 추출 (3) PII 마스킹을 동시에 처리한다
# (추가 모델 호출 없음). 사진/문서를 실제로 보는 단계는 Lambda A뿐이라 사전검증도 여기서만 가능.
# 마스킹 대상: 이름·주민등록번호·외국인등록번호·전화번호·주소·계좌번호 (형식: [항목-마스킹])
ALLOWED_DOC_TYPES = {"LABOR_CONTRACT", "PAY_STUB"}

EXTRACT_MASK_PROMPT = (
    "먼저 이 이미지가 어떤 문서인지 판정해, 첫 줄에 정확히 'DOCTYPE: <코드>' 형식으로만 출력하라.\n"
    "- LABOR_CONTRACT: 근로계약서\n"
    "- PAY_STUB: 급여명세서(임금명세서)\n"
    "- INVALID: 위 둘 중 어느 것도 아닌 모든 경우(신분증, 인물/풍경 사진, 영수증, 무관한 문서 등)\n"
    "판정이 INVALID이면 첫 줄(DOCTYPE: INVALID)만 출력하고 본문은 절대 출력하지 마라.\n"
    "LABOR_CONTRACT 또는 PAY_STUB이면, 둘째 줄부터 문서의 모든 텍스트를 추출하되 다음 개인정보는 "
    "[항목-마스킹] 형식으로 가려라: 이름, 주민등록번호, 외국인등록번호, 전화번호, 주소, 계좌번호. "
    "예: 이름 → [이름-마스킹], 계좌번호 → [계좌번호-마스킹]. "
    "표 구조와 조항 번호는 그대로 보존하라."
)

# Bedrock Converse가 지원하는 포맷 매핑 (매직바이트 → (kind, format))
#   image: png/jpeg/gif/webp · document: pdf
_MAGIC = [
    (b"%PDF", ("document", "pdf")),
    (b"\x89PNG\r\n\x1a\n", ("image", "png")),
    (b"\xff\xd8\xff", ("image", "jpeg")),
    (b"GIF87a", ("image", "gif")),
    (b"GIF89a", ("image", "gif")),
]


def handler(event, context):
    """S3 ObjectCreated 이벤트 진입점. 배치 레코드를 순회한다."""
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        # S3 이벤트 key는 URL 인코딩되어 있음 (공백 등)
        key = unquote_plus(record["s3"]["object"]["key"])
        results.append(_process_object(bucket, key))
    return {"processed": results}


def _process_object(bucket, key):
    logger.info("triggered: s3://%s/%s", bucket, key)

    # masked/ 산출물이 다시 트리거를 일으키는 재귀 방지
    if key.startswith(MASKED_PREFIX):
        logger.info("skip masked artifact: %s", key)
        return {"key": key, "skipped": "masked-artifact"}

    meta = _read_metadata(bucket, key)
    source = meta.get("source")
    document_id = meta.get("document_id")
    if not source or not document_id:
        # 데모 정책: 로그 + 예외 → 원본 유지(7일 수명주기). Lambda B 미호출.
        logger.error(
            "missing required metadata (source/document_id); meta=%s key=%s", meta, key
        )
        raise ValueError("required object metadata missing (source/document_id)")

    try:
        doc_type, masked_text = _extract_and_mask(bucket, key)
    except Exception:
        # 데모 정책: 구조화 로그 + 예외 전파(원본 S3 유지). 운영 전환 시 source 경로 FAILED.
        logger.exception("VLM extract/mask failed: document_id=%s key=%s", document_id, key)
        raise

    # 사전검증: 근로계약서·급여명세서가 아닌 이상한 사진/문서는 분석을 진행하지 않는다.
    # PII 정책: 마스킹본을 만들지 않고 원본도 삭제하지 않는다(원본은 7일 수명주기로 자동 소멸).
    # 단, 프론트 로딩이 무한 대기에 빠지지 않고 'fail'을 받으려면 status를 갱신하는
    # Lambda B 경로로 FAILED를 흘려보내야 한다(A는 SQS/DB 권한이 없어 직접 전송 불가).
    if doc_type == "INVALID":
        logger.warning(
            "precheck rejected (unsupported document): document_id=%s key=%s", document_id, key
        )
        _invoke_lambda_b(
            {
                "document_id": document_id,
                "s3_key": key,
                "masked_text": "",
                "masked_file_url": "",
                "user_lang": meta.get("user_lang") or "ko",
                "source": source,
                "result_queue_arn": meta.get("result_queue_arn") or "",
                "analysis_document_type": meta.get("analysis_document_type") or "UNKNOWN",
                "precheck_failed": True,
                "failed_reason": "unsupported document type (not a labor contract or pay stub)",
            }
        )
        logger.info("dispatched FAILED precheck to Lambda B: document_id=%s", document_id)
        return {"key": key, "document_id": document_id, "precheck_failed": True}

    if not masked_text:
        logger.error("empty masked text: document_id=%s key=%s", document_id, key)
        raise ValueError("masked text is empty")

    masked_file_url = _upload_masked(bucket, document_id, masked_text)

    # 마스킹본 영속화 성공 → 원본 S3 삭제 (PII Layer 3). B는 masked_text로만 분석.
    _delete_original(bucket, key)

    _invoke_lambda_b(
        {
            "document_id": document_id,
            "s3_key": key,
            "masked_text": masked_text,
            "masked_file_url": masked_file_url,
            "user_lang": meta.get("user_lang") or "ko",
            "source": source,
            "result_queue_arn": meta.get("result_queue_arn") or "",
            "analysis_document_type": meta.get("analysis_document_type") or "UNKNOWN",
        }
    )

    logger.info("dispatched to Lambda B: document_id=%s", document_id)
    return {"key": key, "document_id": document_id, "dispatched": True}


# ── 메타데이터 ─────────────────────────────────────────────────────────
def _read_metadata(bucket, key):
    """head_object의 Metadata를 백엔드 계약 키로 정규화.

    주의: head_object 응답 Metadata 딕셔너리는 `x-amz-meta-` 접두사가 빠지고
    소문자로 내려온다. 하이픈/언더스코어 정규화는 S3·SDK 환경에 따라 다를 수
    있어 두 형태를 모두 받는다. (docs/sb-ai-pipeline-handoff.md §5·§6-A)
    TODO(배포): 실제 PUT된 키 이름을 아래 로그로 확인해 백엔드와 1:1 통일.
    """
    head = s3.head_object(Bucket=bucket, Key=key)
    raw = head.get("Metadata", {})
    logger.info("raw object metadata keys=%s", list(raw.keys()))

    def pick(*names):
        for n in names:
            if n in raw:
                return raw[n]
        return None

    return {
        "source": pick("source"),
        "document_id": pick("document_id", "document-id"),
        "result_queue_arn": pick("result_queue_arn", "result-queue-arn"),
        "user_lang": pick("user_lang", "user-lang"),
        # Lambda B가 유형별 분석에 사용. 백엔드가 메타데이터로 심어주면 relay한다.
        # TODO(계약): 백엔드 POST /documents가 x-amz-meta-document-type(또는 analysis-document-type)
        #             을 서명 헤더에 포함하도록 합의 필요. 없으면 B가 UNKNOWN으로 처리.
        "analysis_document_type": pick(
            "analysis_document_type", "analysis-document-type",
            "document_type", "document-type",
        ),
    }


# ── 추출 + 마스킹 (Bedrock Vision 1회) ─────────────────────────────────
def _extract_and_mask(bucket, key):
    """원본을 Bedrock Claude Vision으로 1회 호출해 추출+마스킹 텍스트 반환.

    PII Layer 2: 원본 바이트는 이 함수 스코프를 벗어나지 않고, 호출 직후 del 한다.
    """
    obj = s3.get_object(Bucket=bucket, Key=key)
    raw_bytes = obj["Body"].read()

    content_block = _build_content_block(raw_bytes, key)

    resp = bedrock.converse(
        modelId=MODEL_ID,
        messages=[
            {
                "role": "user",
                "content": [content_block, {"text": EXTRACT_MASK_PROMPT}],
            }
        ],
        # TODO: maxTokens/temperature는 실제 문서 길이 측정 후 조정
        inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": 0},
    )

    # 원본 바이트 즉시 소멸 (메모리 잔존 최소화 — content_block 참조도 함께 해제)
    del raw_bytes
    del content_block

    return _split_verdict(_converse_text(resp))


def _split_verdict(text):
    """모델 응답 첫 줄의 DOCTYPE 판정을 분리해 (doc_type, masked_text)로 반환.

    - LABOR_CONTRACT / PAY_STUB → (코드, 둘째 줄 이후 마스킹 본문).
    - INVALID 또는 알 수 없는 코드 → ("INVALID", ""). 호출부가 분석을 중단한다.
    - 판정 줄 형식 위반 → ("UNKNOWN_FORMAT", 전체 텍스트). 오탐으로 정상 문서를 막지
      않도록 보수적으로 통과시킨다(하드 실패는 모델이 명시적으로 INVALID라 답할 때만).
    """
    first, _, rest = text.partition("\n")
    m = re.match(r"\s*DOCTYPE:\s*([A-Z_]+)", first)
    if not m:
        logger.warning("verdict line missing — proceeding without precheck rejection")
        return "UNKNOWN_FORMAT", text.strip()
    code = m.group(1)
    if code in ALLOWED_DOC_TYPES:
        return code, rest.strip()
    return "INVALID", ""


def _build_content_block(raw_bytes, key):
    """매직바이트로 포맷을 판별해 Converse image/document 블록 구성.

    업로드 Content-Type이 application/octet-stream으로 고정(api-spec §1)이라
    Content-Type/확장자에 의존하지 않고 바이트 시그니처로 판별한다.
    """
    kind, fmt = _detect_format(raw_bytes, key)
    if kind == "document":
        return {
            "document": {
                "format": fmt,
                "name": "doc",  # 영숫자/공백/하이픈/괄호만 허용 (Converse 제약)
                "source": {"bytes": raw_bytes},
            }
        }
    return {"image": {"format": fmt, "source": {"bytes": raw_bytes}}}


def _detect_format(raw_bytes, key):
    """(kind, format) 반환. 시그니처 우선, 실패 시 확장자, 그래도 모르면 png."""
    head = raw_bytes[:16]
    for sig, kf in _MAGIC:
        if head.startswith(sig):
            return kf
    # WEBP: RIFF....WEBP
    if head[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP":
        return ("image", "webp")

    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    fallback = {
        "pdf": ("document", "pdf"),
        "png": ("image", "png"),
        "jpg": ("image", "jpeg"),
        "jpeg": ("image", "jpeg"),
        "gif": ("image", "gif"),
        "webp": ("image", "webp"),
    }
    if ext in fallback:
        return fallback[ext]

    logger.warning("unknown file format (sig+ext fail), defaulting to png: key=%s", key)
    return ("image", "png")


def _converse_text(resp):
    """Converse 응답에서 본문 텍스트만 합쳐 반환."""
    parts = resp.get("output", {}).get("message", {}).get("content", [])
    return "".join(p.get("text", "") for p in parts).strip()


# ── 마스킹본 업로드 ────────────────────────────────────────────────────
def _upload_masked(bucket, document_id, masked_text):
    """마스킹 텍스트를 masked/{document_id}.txt 로 저장하고 s3:// URL 반환."""
    masked_key = f"{MASKED_PREFIX}{document_id}.txt"
    s3.put_object(
        Bucket=bucket,
        Key=masked_key,
        Body=masked_text.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )
    return f"s3://{bucket}/{masked_key}"


# ── 원본 S3 삭제 (PII Layer 3) ─────────────────────────────────────────
def _delete_original(bucket, key):
    """업로드된 원본을 삭제한다. 반드시 _upload_masked 성공 이후에만 호출.

    삭제 자체는 best-effort: 실패해도 7일 수명주기가 백스톱이므로 예외를 전파하지
    않고 로그만 남긴다(B invoke까지 진행). 이미 마스킹본이 저장돼 있어 데이터 유실 없음.
    """
    try:
        s3.delete_object(Bucket=bucket, Key=key)
        logger.info("deleted S3 original (PII Layer 3): s3://%s/%s", bucket, key)
    except Exception:
        logger.exception("failed to delete S3 original (non-fatal): s3://%s/%s", bucket, key)


# ── Lambda B 비동기 invoke ─────────────────────────────────────────────
def _invoke_lambda_b(payload):
    """Lambda B를 비동기(Event)로 발사하고 완료를 기다리지 않는다."""
    if not LAMBDA_B_NAME:
        logger.warning("LAMBDA_B_NAME not set — skip invoke (document_id=%s)", payload.get("document_id"))
        return
    lambda_client.invoke(
        FunctionName=LAMBDA_B_NAME,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
