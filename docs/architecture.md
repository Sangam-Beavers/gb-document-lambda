# 아키텍처 (Architecture)

> Claude Code가 "코드가 어디서, 어떻게 돌아가는지"를 이해하기 위한 문서.
> 인프라 세부 IP/서브넷은 팀 인프라 문서를 따르며, 여기서는 개발에 필요한 수준만 정리한다.

---

## 1. 전체 그림

Global Bridge는 **MSA(마이크로서비스)** 구조를 Kubernetes 위에서 운영한다.

```
인터넷
  ↓
CloudFront + WAF            (DDoS / SQL Injection / XSS 1차 차단)
  ↓
API Gateway                (JWT 서명 검증, Rate Limiting)
  ↓
ALB                        (HTTPS 443만, AZ 분산)
  ↓
EKS (프라이빗 서브넷)       ← 실제 서비스 (금융/문서/커뮤니티)
  ↓
Aurora MySQL (데이터 서브넷)
  +
Redis                      (분산 락, 캐시, 세션 등)
```

트래픽은 반드시 위 레이어를 순서대로 통과해야만 DB에 닿는다. 우회 경로가 없다.

---

## 2. 환경 구성 (3-tier)

이 프로젝트는 **개발기 · 스테이징기 · 운영기** 세 환경을 가진다. 같은 코드, 다른 설정(profile)으로 동작한다.

| 환경 | 위치 | 인증 Provider | DB |
| --- | --- | --- | --- |
| **개발기 (dev)** | 온프렘 Kubernetes (Rocky Linux + Cilium) | Authentik | 온프렘 MySQL 8.0 |
| **스테이징 (stage)** | AWS EKS (sb-stage-vpc) | Cognito | Aurora MySQL |
| **운영기 (prod)** | AWS EKS (sb-prod-vpc) | Cognito | Aurora MySQL |

> **핵심:** Spring 코드는 환경에 따라 바뀌지 않는다.
> `application-dev.yml` / `application-stage.yml` / `application-prod.yml` 의 `issuer-uri`(JWT) 등 **설정값만 다르게** 둔다.
> JWT의 `sub` 값은 환경마다 다르지만(`authentik|...` vs `ap-northeast-2_...`), DB의 `users.auth_provider_id` 컬럼은 동일하게 사용한다.

---

## 3. AWS 계정 분리 (계정 A / 계정 B)

| 계정 | 역할 | 주요 리소스 |
| --- | --- | --- |
| **계정 A** | 운영 서비스(백엔드) | EKS, Aurora MySQL, ALB, Redis, SQS |
| **계정 B** | AI 서류 분석 + 후속 챗봇 전용 | Lambda(A/B), **챗봇 Lambda**, Bedrock, **Bedrock Knowledge Bases(법령 RAG)** + S3 Vectors(KB 백엔드), S3, **DynamoDB(챗봇 대화기록)**, **챗봇 전용 Redis** |

분석 작업은 계정 B에서 격리되어 돌아간다. 자세한 파이프라인은 [`document-analysis/ai-pipeline.md`](./document-analysis/ai-pipeline.md), 후속 챗봇은 [`document-analysis/ai-chatbot-mcp.md`](./document-analysis/ai-chatbot-mcp.md) 참고.

> ⚠️ **저장 정책 (확정):** AI **분석 결과**는 **계정 A의 MySQL `document_results`에 직접 저장**한다. **분석 결과 저장에는 DynamoDB를 사용하지 않는다.**
> ➕ **단, 챗봇 대화기록은 예외:** 후속 질문 챗봇의 대화기록은 분석 결과와 **무관한 별도 워크로드**라, 계정 B에 **DynamoDB(`chat_sessions`, TTL 90일) + Redis 캐시(TTL 30분)** 로 신규 도입한다. 이는 위 "분석 결과는 MySQL" 원칙과 충돌하지 않는다(저장 대상이 다름). 상세: [`document-analysis/ai-chatbot-mcp.md`](./document-analysis/ai-chatbot-mcp.md).
> ➕ **법령 RAG는 Bedrock Knowledge Bases로 통일:** 분석 파이프라인과 챗봇 **양쪽 모두** 법령 검색을 KB `retrieve`로 호출한다. KB의 벡터 저장소(백엔드)는 S3 Vectors다. 즉 S3 Vectors는 빠지지 않고 KB 아래에 깔린다.

---

## 4. 서비스(도메인) 분리

MSA의 각 서비스는 도메인 경계로 나뉜다. 현재는 **단일 Aurora 안에 스키마만 분리**한 상태다(물리 분리는 미래 과제).

> **서비스는 4개다: `member` / `wallet` / `document` / `community`.** 송금(`/transfers`)은 별도 서비스가 아니라 **wallet-service 내부 도메인**이다(주머니·잔액·충전·환전·송금이 한 금융 트랜잭션 경계를 공유하므로 같은 서비스/스키마에 둔다). CLAUDE.md §1·§2의 서비스 목록과 일치한다.

| 도메인(서비스) | 책임 | API prefix |
| --- | --- | --- |
| member | 회원/인증/프로필/설정 | `/auth`, `/members` |
| wallet | 주머니/잔액/거래내역/**송금**/충전/환전 | `/wallets`, `/transfers`, `/exchanges`, `/accounts` |
| document | AI 서류 분석 | `/documents` |
| community | 게시글/댓글/신고/온도 | `/community` |

> **MSA 경계 참조 규칙 (중요):** member 도메인 **밖**에서 회원을 가리킬 때는 `users.id`(BIGINT)가 아니라 **`user_public_id`(UUID, 물리 FK 없음)** 로만 참조한다. 상세는 [`database.md`](./database.md).

### 충전/출금 — Mock 가상 은행

구현 수준 2(Mock API)에 따라, 실제 PG/은행 대신 **가상 은행(Beaver Bank / Quokka Bank)** 을 Mock으로 둔다.

```
충전: 사용자 계좌 → [Mock PG / Beaver Bank] → 주머니 포인트 +P
출금/송금: 주머니 포인트 -P → [Mock 은행 API] → 외부 계좌
```

실서비스 전환 시 Mock URL만 실제 PG/은행 API URL로 교체하면 비즈니스 로직은 그대로 동작하도록 설계한다.

---

## 5. 외부/내부 연동 요약

**아웃바운드(서비스 → 외부)**
- 환율 API(ExchangeRate-API 등), Open Banking 등은 NAT GW를 통해 호출 (EKS IP 비노출)
- AWS 서비스(S3, Bedrock 등)는 VPC 엔드포인트로 내부망 통신

**계정 A ↔ 계정 B**
- 서류 분석 결과는 **요청 출처(`source` 필드)에 따라 한 경로로만** 돌아간다(동시 전송 아님). 백엔드가 `POST /documents` 처리 시 `source`(production/development)를 S3 오브젝트 메타데이터에 심고, Lambda B가 이를 보고 분기한다.
  - **운영기 요청 (source="production"):** 계정 B → **SQS 큐 `gb-analysis-results-prod`(크로스 계정)** → prod `SqsConsumer` → prod Aurora MySQL. (API Gateway 29초 타임아웃 회피용 비동기)
  - **개발기 요청 (source="development"):** 계정 B Lambda B → 계정 B EC2(HAProxy) → **WireGuard 터널** → 온프렘 개발기 MySQL 직접 INSERT.
  - **스테이징(stage):** 별도 `source` 값은 없이 `source="production"` 계열을 타되, **prod와는 물리적으로 분리된 SQS 큐 `gb-analysis-results-stage`** 로 결과를 받아 **자기 stage Aurora**에 저장한다. 즉 `source` 매핑은 dev→`development`, **stage·prod→`production`**(인프라 계열만 결정)이고, 어느 Aurora로 갈지는 **환경별 큐**가 결정한다. stage/prod 큐가 분리돼 결과가 환경 간 섞이거나 교차 수신되지 않는다(dev는 온프렘 직결로 이미 격리). Consumer 코드는 동일, 구독 큐 이름만 `application-{stage|prod}.yml`로 분리.
  - 개발기/운영기는 완전 분리 — 요청한 환경으로만 결과가 저장된다.

**관리자 접근**
- SSH 포트 없음. AWS SSM 세션 매니저로만 접근 (인바운드 포트 0)

---

## 6. 예상 트래픽 (인프라 설계 기준)

초기 단계 기준 추정치다. 대규모는 아니지만 이벤트/비동기 구조를 고려해 설계한다.

| 지표 | 추정치 |
| --- | --- |
| 잠재 시장 | 국내 외국인 노동자 약 110만 명 |
| 초기 가입자 | 약 1,000명 (시장의 0.1%) |
| DAU | 약 300~400명 |
| 피크 동시 접속 | 약 20~30명 |

→ EKS Auto Scaling, ALB 분산, Public/Private 분리 보안 구조 적용. 단순 CRUD가 아닌 비동기/이벤트 처리 구조 고려.

---

## 7. 보안 핵심 원칙 (개발 시 반드시 반영)

- **다층 방어**: WAF → API GW(JWT) → ALB → Spring Security(2차 인가) → DB
- **public_id 노출**: API 응답·URL에 내부 순번 `id` 노출 금지. UUID(`public_id`)만 사용
- **민감정보 암호화**: 신분증 번호 등은 AES-256 저장(또는 해시), 원본 최소 보관
- **금융 무결성**: 잔액은 캐싱 금지, 금액은 DECIMAL, 거래는 멱등성 키 + Redis 분산 락
- **감사 로그**: `transaction_audit_logs`는 append-only (INSERT만)
