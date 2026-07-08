# 변경 내역 — collect.py 수집 방식 교체 (Playwright → HTTP)

## 한 줄 요약
클라우드 실행 환경에서 헤드리스 브라우저가 프록시를 통과하지 못해 수집이 전면 실패하던 문제를, **브라우저를 제거하고 X GraphQL 웹 API를 HTTP(`requests`)로 직접 호출**하도록 교체해 해결. 라이브 검증 완료.

## 근본 원인
- 기존 `collect.py`는 헤드리스 **Chromium(Playwright)**으로 `x.com/{계정}`을 열어 GraphQL 응답을 가로채 파싱했다.
- 이 클라우드 환경은 모든 아웃바운드가 **에이전트 프록시(`HTTPS_PROXY`)**를 거쳐야 x.com에 닿는다.
- 그런데 **브라우저만** 프록시 경유 연결이 `net::ERR_CONNECTION_CLOSED`로 끊긴다(프록시가 브라우저 TLS 핸드셰이크를 차단; `curl`·소켓·`requests`는 정상).
- 같은 프록시로 `requests`는 x.com에 **HTTP 200**으로 정상 도달 → egress 정책은 x.com을 **허용**한다. 즉 **X 차단도, 계정 문제도 아니었다.** (다계정 불필요.)

## 변경 파일

### `collect.py`
- **수집부 교체**: `Playwright`(브라우저 구동·스크롤·응답 가로채기) → `requests` 기반 **X GraphQL 직접 호출**.
  - `UserByScreenName`로 계정 `rest_id` 조회 → `UserTweets`로 타임라인 수신.
  - `HTTPS_PROXY` / `REQUESTS_CA_BUNDLE`는 `requests`가 환경변수에서 **자동 인식**(클라우드 프록시·CA 그대로 적용, 로컬에선 그냥 직결).
- **재사용**: 기존 파서 `_extract_tweets`, 윈도우 계산 `compute_window`, 출력 스키마(`new_tweets.json`)는 **그대로**. 인용/리트윗 원글(`ref_*`) 첨부 로직도 동일.
- **인증 오류 처리**: 401/403 또는 X 에러코드(32/63/64/89/215)를 `CookieExpired`로 감지 → `"X 쿠키 무효 — 쿠키 재추출 필요"` 출력 후 `exit 3` (지침 Step A의 쿠키 만료 보고와 동일).
- **견고성**: GraphQL 호출에 타임아웃·재시도(429/네트워크 백오프) 추가.
- **상수화**: 공개 웹 베어러·`USER_BY_SCREEN_NAME_QID`·`USER_TWEETS_QID`·`FEAT_*`를 파일 상단 상수로. X가 queryId를 교체하면 **이 상수들만 갱신**하면 됨.
- `async`/`asyncio` 제거(동기 코드로 단순화). 미사용 `GRAPHQL` 상수 삭제.
- 헤더 docstring의 "1시간" → "WINDOW_HOURS(현재 3)" 표기 정정.

### `requirements.txt`
- `requests>=2.31.0` 추가.
- `playwright` 제거 (2026-07-08): collect.py 가 requests 기반으로 전환돼 불필요해졌고,
  셋업 스크립트의 `playwright install --with-deps`(apt-get update 포함)가 베이스 이미지의
  ondrej/php PPA Label 변경으로 exit 100 실패를 유발했음. 브라우저 설치 자체가 불필요하므로
  의존성에서 삭제. Playwright 전용 진단 스크립트 `x_access_test.py` 도 함께 삭제
  (검증 목적은 이미 달성, 결론은 본 문서 "근본 원인" 참조).

## 인터페이스 — 변경 없음 (중요)
- 실행 명령 동일: `python collect.py`
- 산출물 동일: `new_tweets.json` (키·필드·`repost_kind`/`ref_*` 전부 동일)
- 쿠키 만료 동작 동일: 메시지 보고 후 종료
- → **Step B~F 및 send_tg.py 영향 없음.**

## 부가 효과
- 브라우저 미구동으로 **더 가볍고 빠름**(로컬 PC 부하도 더 적음).
- 로컬·클라우드 양쪽에서 동일 동작.

## 운영/유지보수 메모
- 의존성 설치 시 `pip install -r requirements.txt`에 `requests`가 포함됨(별도 조치 불필요).
- 향후 수집이 다시 실패하면 두 가지만 점검:
  1. **쿠키 만료** → 로그에 "쿠키 재추출 필요" 출력 시 `X_COOKIES_JSON` 갱신.
  2. **queryId 교체** → X가 GraphQL queryId를 바꾸면 404/오류. `collect.py` 상단 `USER_BY_SCREEN_NAME_QID` / `USER_TWEETS_QID` 갱신.

## 루틴 지시사항(MD) 변경 필요 여부
- **필수 변경 없음** — collect.py의 명령·출력·쿠키오류 동작이 모두 동일하므로 기존 지침 그대로 유효.
- (선택) 「## 제약」에 유지보수 메모 한 줄 추가 권장:
  > 수집 실패 시: ① "쿠키 재추출 필요" 로그면 X_COOKIES_JSON 갱신, ② 그 외 GraphQL 오류면 collect.py 상단 *_QID(queryId) 갱신.
