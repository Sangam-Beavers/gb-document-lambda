"""로컬 스모크 테스트 (boto3 스텁). 순수 헬퍼/조립만 검증. 실행: python lambda_b/_smoke_test.py"""
import os
import sys
import types

fake = types.ModuleType("boto3")
fake.client = lambda *a, **k: None
sys.modules["boto3"] = fake
sys.path.insert(0, os.path.dirname(__file__))

import handler as h  # noqa: E402

# ARN → Queue URL
url = h._arn_to_queue_url("arn:aws:sqs:ap-northeast-2:123456789012:gb-analysis-results-prod")
assert url == "https://sqs.ap-northeast-2.amazonaws.com/123456789012/gb-analysis-results-prod", url

try:
    h._arn_to_queue_url("arn:aws:s3:::bucket")
    raise AssertionError("should reject non-sqs ARN")
except ValueError:
    pass

# ISO 8601 UTC Z 형식
now = h._utc_now_iso()
assert now.endswith("Z") and "T" in now and len(now) == 20, now

# 결과 조립 — COMPLETED는 completed_at 채움
event = {
    "document_id": "doc-1",
    "analysis_document_type": "LABOR_CONTRACT",
    "masked_file_url": "s3://b/masked/doc-1.txt",
    "masked_text": "secret",
}
res = h._build_result(event, {
    "processing_status": "COMPLETED",
    "overall_risk_level": "HIGH",
    "risk_items": [{"risk_level": "HIGH", "clause": "c", "description": "d"}],
})
assert res["document_public_id"] == "doc-1"
assert res["processing_status"] == "COMPLETED"
assert res["completed_at"] and res["completed_at"].endswith("Z")
assert res["masked_file_url"] == "s3://b/masked/doc-1.txt"
# v1.1 필수 필드 — schema_version(Consumer 검증) + translated_lang(user_lang 미지정 시 ko)
assert res["schema_version"] == "1.1"
assert res["translated_lang"] == "ko"
assert h._build_result({**event, "user_lang": "vi"}, {"processing_status": "COMPLETED", "risk_items": []})["translated_lang"] == "vi"

# FAILED는 completed_at null
res_f = h._build_result(event, {"processing_status": "FAILED", "risk_items": [], "failed_reason": "x"})
assert res_f["completed_at"] is None
assert res_f["failed_reason"] == "x"

# s3:// URI → key (온프렘 s3_masked_key 컬럼은 경로만 저장)
assert h._s3_uri_to_key("s3://b/masked/doc-1.txt") == "masked/doc-1.txt", h._s3_uri_to_key("s3://b/masked/doc-1.txt")
assert h._s3_uri_to_key("masked/doc-1.txt") == "masked/doc-1.txt"
assert h._s3_uri_to_key(None) is None
assert h._s3_uri_to_key("") is None

# redact는 masked_text 제거
assert "masked_text" not in h._redact(event)
assert h._redact(event)["document_id"] == "doc-1"

print("all helper assertions passed")
