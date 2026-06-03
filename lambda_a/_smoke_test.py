"""로컬 스모크 테스트 (boto3 스텁). 순수 헬퍼만 검증. 실행: python lambda_a/_smoke_test.py"""
import os
import sys
import types

fake = types.ModuleType("boto3")
fake.client = lambda *a, **k: None
sys.modules["boto3"] = fake
sys.path.insert(0, os.path.dirname(__file__))

import handler as h  # noqa: E402

assert h._detect_format(b"%PDF-1.7 abc", "x") == ("document", "pdf")
assert h._detect_format(b"\x89PNG\r\n\x1a\n", "x") == ("image", "png")
assert h._detect_format(b"\xff\xd8\xff\xe0", "x") == ("image", "jpeg")
assert h._detect_format(b"GIF89a", "x") == ("image", "gif")
assert h._detect_format(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "x") == ("image", "webp")
assert h._detect_format(b"????", "scan.PDF") == ("document", "pdf")
assert h._detect_format(b"????", "noext") == ("image", "png")

assert h._converse_text(
    {"output": {"message": {"content": [{"text": "a"}, {"text": "b"}]}}}
) == "ab"
assert h._converse_text({}) == ""

doc = h._build_content_block(b"%PDF-1.7", "x")
assert doc["document"]["format"] == "pdf"
img = h._build_content_block(b"\x89PNG\r\n\x1a\n", "x")
assert img["image"]["format"] == "png"

print("all helper assertions passed")
