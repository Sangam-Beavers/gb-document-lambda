"""Lambda A end-to-end 흐름 검증 (boto3 스텁). 실행: python lambda_a/_verify_flow.py

검증 대상(요청 사양): S3 ObjectCreated → head_object → get_object → Converse(OCR+PII)
  → 마스킹본 masked/ 업로드 → original/ 원본 삭제(PII Layer 3) → Lambda B 비동기 invoke.
핵심 단언:
  · 원본 삭제(delete_object)가 마스킹본 업로드(put_object) **이후**에 일어난다(순서 보장 = 데이터 유실 방지).
  · 삭제 대상 키 == 트리거된 original 키(마스킹본 masked/ 키가 아님).
  · 삭제는 best-effort: delete_object가 실패해도 예외 전파 없이 B invoke까지 진행.
"""
import json
import os
import sys
import types

calls = {"s3": [], "bedrock": [], "lambda": []}


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeS3:
    def __init__(self, delete_raises=False):
        self._delete_raises = delete_raises

    def head_object(self, Bucket, Key):
        calls["s3"].append(("head_object", Bucket, Key))
        return {"Metadata": {
            "source": "production",
            "document-id": "doc-123",
            "result-queue-arn": "arn:aws:sqs:ap-northeast-2:111122223333:gb-analysis-results-prod",
            "user-lang": "ko",
            "document-type": "LABOR_CONTRACT",
        }}

    def get_object(self, Bucket, Key):
        calls["s3"].append(("get_object", Bucket, Key))
        return {"Body": _Body(b"%PDF-1.7 fake original bytes")}

    def put_object(self, **kw):
        calls["s3"].append(("put_object", kw["Bucket"], kw["Key"], kw["Body"]))

    def delete_object(self, **kw):
        calls["s3"].append(("delete_object", kw["Bucket"], kw["Key"]))
        if self._delete_raises:
            raise RuntimeError("simulated S3 delete failure")


class FakeBedrock:
    def converse(self, **kw):
        calls["bedrock"].append(("converse", kw["modelId"]))
        return {"output": {"message": {"content": [
            {"text": "근로계약서\n성명: [이름-마스킹]\n월급여: 2,000,000원"}
        ]}}}


class FakeLambda:
    def invoke(self, **kw):
        calls["lambda"].append((
            "invoke", kw["FunctionName"], kw["InvocationType"],
            json.loads(kw["Payload"].decode("utf-8")),
        ))


_state = {"delete_raises": False}


def fake_client(service, **kw):
    return {
        "s3": FakeS3(delete_raises=_state["delete_raises"]),
        "bedrock-runtime": FakeBedrock(),
        "lambda": FakeLambda(),
    }[service]


fake = types.ModuleType("boto3")
fake.client = fake_client
sys.modules["boto3"] = fake
os.environ["LAMBDA_B_NAME"] = "gb-lambda-b"
sys.path.insert(0, os.path.dirname(__file__))

import handler as h  # noqa: E402

EVENT = {"Records": [{"s3": {
    "bucket": {"name": "gb-upload"},
    "object": {"key": "original/doc-123.pdf"},
}}]}


def _run():
    calls["s3"].clear()
    calls["bedrock"].clear()
    calls["lambda"].clear()
    # 핸들러는 import 시 캐싱된 모듈 전역 클라이언트를 쓰므로, 케이스별 동작은
    # _state로 토글한 새 인스턴스를 모듈 전역에 주입해 반영한다.
    h.s3 = FakeS3(delete_raises=_state["delete_raises"])
    return h.handler(EVENT, None)


# ════════ 케이스 1: 정상 경로 ════════
_state["delete_raises"] = False
out = _run()

ops = [c[0] for c in calls["s3"]]
assert ops == ["head_object", "get_object", "put_object", "delete_object"], ops

put = next(c for c in calls["s3"] if c[0] == "put_object")
delete = next(c for c in calls["s3"] if c[0] == "delete_object")

# 순서: 마스킹본 put이 원본 delete보다 먼저 (데이터 유실 방지)
assert ops.index("put_object") < ops.index("delete_object")

# 마스킹본은 masked/{id}.txt, PII 마스킹 유지
assert put[1] == "gb-upload" and put[2] == "masked/doc-123.txt", put
assert "[이름-마스킹]" in put[3].decode("utf-8")

# ★ 삭제 대상 == 트리거된 original 키 (마스킹본이 아님!)
assert delete[1] == "gb-upload" and delete[2] == "original/doc-123.pdf", delete
assert delete[2] != put[2], "원본이 아니라 마스킹본을 지우면 안 됨"

# Bedrock 1회, inference profile
assert len(calls["bedrock"]) == 1
assert calls["bedrock"][0][1] == "global.anthropic.claude-sonnet-4-6"

# Lambda B 비동기 invoke + relay 페이로드
assert len(calls["lambda"]) == 1
_, fn, inv_type, payload = calls["lambda"][0]
assert fn == "gb-lambda-b" and inv_type == "Event"
assert payload["document_id"] == "doc-123"
assert payload["masked_file_url"] == "s3://gb-upload/masked/doc-123.txt"
assert payload["source"] == "production"
assert payload["result_queue_arn"].endswith("gb-analysis-results-prod")
assert payload["analysis_document_type"] == "LABOR_CONTRACT"
assert "[이름-마스킹]" in payload["masked_text"]

assert out == {"processed": [{"key": "original/doc-123.pdf",
                              "document_id": "doc-123", "dispatched": True}]}, out

# ════════ 케이스 2: 삭제 실패 = best-effort (예외 전파 없이 B invoke 진행) ════════
_state["delete_raises"] = True
out2 = _run()
ops2 = [c[0] for c in calls["s3"]]
assert "delete_object" in ops2                 # 삭제 시도함
assert len(calls["lambda"]) == 1               # 삭제 실패에도 B invoke는 진행
assert out2["processed"][0]["dispatched"] is True

# ════════ 케이스 3: masked/ 산출물 재트리거 재귀 방지 ════════
calls["s3"].clear()
h.s3 = FakeS3()
out3 = h._process_object("gb-upload", "masked/doc-123.txt")
assert out3 == {"key": "masked/doc-123.txt", "skipped": "masked-artifact"}
assert calls["s3"] == []                        # head_object조차 안 함 (삭제도 당연히 안 함)

print("OK [1] OCR+PII -> masked/ put -> original/ delete -> Lambda B Event invoke (순서/대상키 검증)")
print("OK [2] 원본 삭제 실패해도 best-effort (예외 없이 B invoke 진행)")
print("OK [3] masked/ 산출물은 재트리거 스킵 (원본 삭제 오발 방지)")
