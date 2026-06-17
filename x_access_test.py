"""
클라우드 X 접근 검증 테스트 (self-contained)
================================================================
목적: Anthropic 클라우드 샌드박스(데이터센터 IP)에서 X 로그인 쿠키가
      여전히 먹히고 트윗 데이터를 받아올 수 있는지 '딱 그것만' 검증.

로컬 의존성 없음(src/ 불필요). 쿠키는 환경변수 X_COOKIES_JSON에서 읽음.
  - X_COOKIES_JSON 예: {"auth_token":"...","ct0":"..."}

판정:
  PASS  → 로그인 OK + 트윗 N개 수신   (클라우드 스크래핑 가능)
  FAIL  → 로그인 페이지로 리다이렉트   (데이터센터 IP 차단 또는 쿠키 무효)
  WARN  → 로그인은 됐으나 트윗 0개     (챌린지/레이트리밋 의심)

사용:
    pip install playwright && python -m playwright install chromium
    X_COOKIES_JSON='{"auth_token":"...","ct0":"..."}' python x_access_test.py
"""
import asyncio
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright

ACCOUNTS = ["dmjk001", "jukan05"]   # 본 작업과 동일 대상
GRAPHQL = ["/UserTweets", "/UserByScreenName", "/SearchTimeline"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def load_cookies() -> dict:
    raw = os.environ.get("X_COOKIES_JSON", "").strip()
    if not raw:
        print("ERROR: 환경변수 X_COOKIES_JSON 이 비어 있음. "
              '{"auth_token":"...","ct0":"..."} 형태로 설정 필요.')
        sys.exit(2)
    try:
        c = json.loads(raw)
    except Exception as e:
        print(f"ERROR: X_COOKIES_JSON 파싱 실패: {e}")
        sys.exit(2)
    if "auth_token" not in c:
        print("ERROR: X_COOKIES_JSON 에 auth_token 없음.")
        sys.exit(2)
    return c


async def run():
    print("=" * 60)
    print("클라우드 X 접근 검증 테스트 시작")
    print("=" * 60)

    cookies = load_cookies()

    async with async_playwright() as pw:
        # 클라우드 샌드박스는 아웃바운드 HTTPS를 TLS 가로채기 프록시로 보냄
        # → 프록시 CA 미신뢰(ERR_CERT_AUTHORITY_INVALID) 회피용 옵션
        browser = await pw.chromium.launch(
            headless=True,
            args=["--ignore-certificate-errors"],
        )
        ctx = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        cobjs = []
        for n, v in cookies.items():
            for dom in [".x.com", ".twitter.com"]:
                cobjs.append({"name": n, "value": str(v), "domain": dom, "path": "/",
                              "secure": True, "httpOnly": n == "auth_token",
                              "sameSite": "Lax"})
        await ctx.add_cookies(cobjs)
        page = await ctx.new_page()

        # ── 1) 로그인(쿠키) 유효성: /home 접속 후 리다이렉트 확인
        print("\n[1] 로그인 쿠키 검증 (x.com/home)...")
        try:
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(4)
        except Exception as e:
            print(f"  나비게이션 실패: {str(e)[:120]}")
            print("\n판정: FAIL (네트워크/타임아웃 — 샌드박스 네트워크 활성화 확인 필요)")
            await browser.close()
            return
        if "/login" in page.url or "/i/flow/login" in page.url:
            print(f"  → 로그인 페이지로 리다이렉트됨: {page.url}")
            print("\n판정: FAIL (데이터센터 IP 차단 또는 쿠키 무효) — 클라우드 스크래핑 불가")
            await browser.close()
            return
        print(f"  → 로그인 OK: {page.url}")

        # ── 2) 실제 트윗 수신: 계정 타임라인에서 UserTweets GraphQL 가로채기
        total_tweets = 0
        for acc in ACCOUNTS:
            print(f"\n[2] @{acc} 타임라인 데이터 수신 테스트...")
            captured = []

            async def on_resp(r, _c=captured):
                if any(p in r.url for p in GRAPHQL):
                    try:
                        _c.append(await r.text())
                    except Exception:
                        pass

            page.on("response", on_resp)
            try:
                await page.goto(f"https://x.com/{acc}", wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(4)
                for _ in range(4):
                    await page.evaluate("window.scrollBy(0, 1800)")
                    await asyncio.sleep(2.5)
            except Exception as e:
                print(f"  @{acc} 나비게이션 실패: {str(e)[:120]}")
            page.remove_listener("response", on_resp)

            blob = "\n".join(captured)
            # 거친 카운트: 타임라인 응답에 들어있는 Tweet 객체 수
            n_tweet = len(re.findall(r'"__typename"\s*:\s*"Tweet"', blob))
            n_ids = len(set(re.findall(r'"rest_id"\s*:\s*"(\d{15,})"', blob)))
            print(f"  GraphQL 응답 {len(captured)}건, Tweet객체≈{n_tweet}, 고유ID≈{n_ids}")
            total_tweets += max(n_tweet, n_ids)
            await asyncio.sleep(6)

        await browser.close()

        # ── 판정
        print("\n" + "=" * 60)
        if total_tweets > 0:
            print(f"판정: PASS (트윗 데이터 수신 OK, 총≈{total_tweets}건)")
            print("→ 클라우드 데이터센터 IP에서 X 스크래핑 가능. 본 마이그레이션 진행 OK.")
        else:
            print("판정: WARN (로그인은 됐으나 트윗 0건)")
            print("→ 챌린지/레이트리밋 의심. 시간대 바꿔 재시도하거나 X API 검토 필요.")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run())
