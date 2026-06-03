# 분석 결과 수신 — 환경별 SQS 큐 분리 (구현 가이드)

> AI 분석 결과가 **요청을 보낸 환경 본인의 DB로만** 돌아가도록 하는 라우팅 규약.
> 상위 설계는 [`ai-pipeline.md`](./ai-pipeline.md) §6·§8, [`../architecture.md`](../architecture.md) §환경 구성 참고.
> 이 문서는 큐 분리 방식 확정에 따른 백엔드 구현 접점만 다룬다.

---

## 1. 결정 요약

- `source`(`development`/`production`)는 **인프라 계열**만 가른다(온프렘 vs AWS).
- `production` 계열 안에서 **stage와 prod는 별도 SQS 큐**로 분리한다.
- 결과는 **요청 환경 본인의 큐 → 본인의 Aurora**로만 저장된다. 교차 수신 불가.
- 단일 큐 + `env` 필드 필터 방식은 **채택하지 않는다**(필터 누락 시 오염 위험).

| 환경 | source | 결과 채널 | 도착 DB | 백엔드 Consumer |
| --- | --- | --- | --- | --- |
| dev | `development` | 온프렘 직결(WireGuard) | 온프렘 개발기 MySQL | 불필요(조회만) |
| stage | `production` | SQS `gb-analysis-results-stage` | stage Aurora | 기동(자기 큐만 구독) |
| prod | `production` | SQS `gb-analysis-results-prod` | prod Aurora | 기동(자기 큐만 구독) |

---

## 2. 요청 시 — 결과 큐 ARN을 메타데이터에 주입

`POST /api/v1/documents` 처리 시, `source`와 함께 **자기 환경의 결과 큐 ARN**을 S3 오브젝트 메타데이터에 심는다. `development`(dev)는 큐를 쓰지 않으므로 ARN을 비운다.

```yaml
# application-dev.yml
gb:
  analysis:
    source: development
    result-queue-arn: ""          # 온프렘 직결, 큐 미사용

# application-stage.yml
gb:
  analysis:
    source: production
    result-queue-arn: arn:aws:sqs:ap-northeast-2:<acct-A>:gb-analysis-results-stage

# application-prod.yml
gb:
  analysis:
    source: production
    result-queue-arn: arn:aws:sqs:ap-northeast-2:<acct-A>:gb-analysis-results-prod
```

Lambda B는 `source=production`이면 메타데이터의 `result_queue_arn`으로만 결과를 발행한다.
→ Lambda는 환경 매핑 테이블을 가질 필요가 없다(백엔드가 ARN을 통째로 넘김).

> ⚠️ 이 메타데이터(`result_queue_arn` 포함)는 Pre-signed PUT URL의 **서명 헤더**에 들어가므로,
> 클라이언트는 업로드 시 `POST /documents` 응답의 `upload_headers`(`x-amz-meta-result_queue_arn` 등)를
> 그대로 PUT에 실어야 한다. 안 보내면 403으로 업로드가 실패해 결과 큐 라우팅 자체가 시작되지 않는다.
> 상세: `api-spec.md` §1.

---

## 3. 수신 시 — 자기 큐만 구독 (코드 동일, 큐 이름만 분리)

Consumer 코드는 환경 무관하게 동일하고, 구독 큐 이름만 프로필로 주입한다.

**수신 라이브러리 결정(2026-05-30):** `spring-cloud-aws-starter-sqs` 3.x + `@SqsListener`. 컨테이너가
폴링 스레드·visibility timeout 연장·ack-on-success를 책임진다. 리스너가 예외를 던지면 컨테이너가
`deleteMessage`를 호출하지 않으므로, 메시지는 visibility timeout 이후 재수신되고 `maxReceiveCount`
초과 시 DLQ로 자동 이동한다. (raw SDK 폴링은 검토했으나 ack 시맨틱을 직접 구현하는 부담이 커
배제 — `docs/document-analysis/result-json-schema-agreement.md` §1 참조.)

**페이로드 위치 결정(2026-05-30):** v1.1 JSON은 **SQS 메시지 본문 그대로**, 라우팅 메타(`source`,
`document_public_id`)는 **SQS MessageAttributes**(envelope JSON 아님). 본문 == 스키마 SSOT가 글자
그대로 유지된다. 자세히는 `result-json-schema-agreement.md` §1 경고문.

```yaml
# application-stage.yml
gb:
  analysis:
    consumer-enabled: true
    consumer-queue-name: gb-analysis-results-stage

# application-prod.yml
gb:
  analysis:
    consumer-enabled: true
    consumer-queue-name: gb-analysis-results-prod

# application-dev.yml  → Consumer 미기동
gb:
  analysis:
    consumer-enabled: false
    consumer-queue-name: ""
```

```java
// 환경별 큐 이름만 주입받아 구독. 로직은 동일.
// body는 v1.1 결과 JSON 그대로 → Jackson이 DTO로 역직렬화.
// source / document_public_id는 SQS MessageAttributes에서 @Header로 수신.
// spring-cloud-aws 3.x SqsHeaderMapper는 사용자 message attribute를 헤더로 매핑할 때 접두사를
// 붙이지 않고 attribute 키를 그대로 헤더 키로 쓴다(시스템 attribute만 "Sqs_Msa_" 접두사가 붙음).
// 따라서 @Header("source"), @Header("document_public_id")로 직접 받는다. @SqsListener 어노테이션에는
// messageAttributeNames 속성이 없으므로 SqsContainerOptions 레벨(아래 팩토리 빈)에서 명시 요청.
@SqsListener("${gb.analysis.consumer-queue-name}")
public void onAnalysisResult(
        AnalysisResultMessage msg,                                  // body
        @Header("source") String source,                            // attribute (prefix 없음)
        @Header("document_public_id") String routingId              // attribute (prefix 없음)
) {
    // 1) document_submissions 조회 (msg.documentPublicId 기준, attribute 값과 일치 검증)
    // 2) document_results UPSERT (submission_id UNIQUE로 멱등)
    // 3) document_submissions.status 동기화 (COMPLETED/FAILED, PARTIAL→COMPLETED)
    // (S3 원본 삭제는 Consumer 책임 아님 — Lambda A가 마스킹 직후 자기 버킷의 원본을 삭제. ai-pipeline.md §6·§8)
}

// 컨테이너 옵션에서 어떤 attribute를 ReceiveMessage로 요청할지 명시.
// auto-config의 동명 빈을 대체한다(빈 이름: defaultSqsListenerContainerFactory).
@Bean
SqsMessageListenerContainerFactory<Object> defaultSqsListenerContainerFactory(
        SqsAsyncClient sqsAsyncClient) {
    return SqsMessageListenerContainerFactory.builder()
            .sqsAsyncClient(sqsAsyncClient)
            .configure(opts -> opts.messageAttributeNames(List.of("source", "document_public_id")))
            .build();
}
```

dev는 Consumer를 기동하지 않는다(`@ConditionalOnProperty("gb.analysis.consumer-enabled")` =
stage·prod만 true). dev 백엔드는 온프렘 MySQL을 **조회만** 한다.

> stage 큐와 prod 큐는 물리적으로 다른 리소스이므로, 한 환경 Consumer가 다른 환경
> 메시지를 받는 것이 구조적으로 불가능하다. 환경 간 데이터 오염 경로가 없다.

> ⚠️ {@code SqsContainerOptions.messageAttributeNames}를 명시하지 않으면 spring-cloud-aws는
> {@code ReceiveMessage} 호출 시 attribute 리스트를 보내지 않아 attribute가 누락된 채 도착한다.
> 회귀 테스트로 두 헤더 주입을 검증한다.

---

## 4. 인프라(IaC) 체크리스트 — 미확정 항목

- [ ] 계정 A에 SQS 큐 2개 생성: `gb-analysis-results-stage`, `gb-analysis-results-prod`
- [ ] 각 큐 + DLQ 1개씩(재처리). 가시성 타임아웃은 Consumer 처리시간 + 여유.
- [ ] 계정 B Lambda B의 IAM에 두 큐 `sqs:SendMessage` 크로스계정 허용(큐 정책 양쪽).
- [ ] stage/prod 백엔드 IRSA에 **자기 큐만** `sqs:ReceiveMessage/DeleteMessage` 부여(최소권한).
- [ ] S3 메타데이터 키 `result_queue_arn` Lambda A→B 전달 경로 확인(메타데이터 그대로 relay).
- [ ] dev 경로(WireGuard)는 큐 무관 — 변경 없음.
