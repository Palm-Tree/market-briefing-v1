#!/usr/bin/env python3
"""
매일 아침 시장 브리핑 생성 + 스마트폰 푸시(ntfy)

데이터 출처 (모두 무료)
  - Yahoo Finance (yfinance, 비공식): 지수, 환율, 달러인덱스, 10년물(^TNX), WTI, 금, VIX (+52주 범위)
  - 미 재무부 공개 CSV(키 불필요): 2년물/10년물 일 종가
  - 공공데이터포털 금융위 "일반상품시세정보"(무료 키): KRX 금시장 종가 (선택)
  - Anthropic API (선택): 핵심 포인트 3~5개 자동 해석. 없으면 규칙 기반 요약

환경변수
  NTFY_TOPIC         ntfy 토픽 이름 (추측하기 어려운 긴 문자열 권장)
  DATA_GO_KR_KEY     공공데이터포털 인증키 (선택)
  ANTHROPIC_API_KEY  (선택) 있으면 Claude가 핵심 포인트 작성

사용법
  pip install yfinance requests
  python briefing.py --dry-run     # 푸시 없이 화면에만 출력
  python briefing.py               # 푸시 전송
"""
import csv
import io
import os
import sys
import datetime as dt

import requests
import yfinance as yf

KST = dt.timezone(dt.timedelta(hours=9))
TIMEOUT = 20

# (섹션, 표시이름, 야후 티커, 소수점)
YAHOO_ITEMS = [
    ("주가지수", "S&P500", "^GSPC", 2),
    ("주가지수", "나스닥 종합", "^IXIC", 2),
    ("주가지수", "나스닥100", "^NDX", 2),
    ("주가지수", "다우존스", "^DJI", 2),
    ("주가지수", "러셀2000", "^RUT", 2),
    ("주가지수", "코스피", "^KS11", 2),
    ("주가지수", "VIX", "^VIX", 2),
    ("환율·금리", "원/달러", "KRW=X", 2),
    ("환율·금리", "달러 인덱스", "DX-Y.NYB", 2),
    ("환율·금리", "미 10년물(%)", "^TNX", 3),
    ("원자재", "WTI($)", "CL=F", 2),
    ("원자재", "국제 금($/oz)", "GC=F", 2),
]


def fetch_yahoo(ticker):
    """1년치 일봉으로 현재가/전일比/52주 범위 계산.
    장중이면 당일 봉(지연 시세)이 마지막 행. 52주 범위는 매 실행마다 최근 1년으로 다시 계산되고,
    현재가가 기존 최고/최저를 넘으면 범위도 함께 갱신된다."""
    h = yf.Ticker(ticker).history(period="1y", interval="1d", auto_adjust=False)
    h = h.dropna(subset=["Close"])
    last, prev = h.iloc[-1], h.iloc[-2]
    value, prev_v = float(last["Close"]), float(prev["Close"])
    lo, hi = float(h["Low"].min()), float(h["High"].max())
    if ticker == "^TNX" and value > 20:  # 구버전 yfinance는 x10 단위
        value, prev_v, lo, hi = value / 10, prev_v / 10, lo / 10, hi / 10
    last_date = h.index[-1].date()
    today_local = dt.datetime.now(h.index.tz).date() if h.index.tz else dt.date.today()
    status = "장중/당일" if last_date == today_local else "종가"
    return {"value": value, "prev": prev_v, "date": last_date, "status": status,
            "lo": min(lo, value), "hi": max(hi, value)}


def _treasury_rows(year):
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
        f"&field_tdr_date_value={year}&page&_format=csv"
    )
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for row in csv.DictReader(io.StringIO(r.text)):
        try:
            out.append((dt.datetime.strptime(row["Date"], "%m/%d/%Y").date(), float(row["2 Yr"])))
        except (ValueError, KeyError):
            continue
    return out


def fetch_treasury_2y():
    """미 재무부 일별 수익률 곡선 CSV (키 불필요, 전 영업일까지 반영). 작년 파일까지 합쳐 52주 범위 계산."""
    year = dt.datetime.now(KST).year
    rows = _treasury_rows(year)
    try:
        rows += _treasury_rows(year - 1)
    except Exception:
        pass
    rows.sort()
    cutoff = rows[-1][0] - dt.timedelta(days=365)
    window = [v for d, v in rows if d >= cutoff]
    return {"value": rows[-1][1], "prev": rows[-2][1], "date": rows[-1][0], "status": "종가",
            "lo": min(window), "hi": max(window)}


KRX_GOLD_URLS = [
    "https://apis.data.go.kr/1160100/GetGeneralProductInfoService_V2/getGoldPriceInfo_V2",
    "https://apis.data.go.kr/1160100/GetGeneralProductInfoService_V2/getGoldPriceInfo",
    "https://apis.data.go.kr/1160100/service/GetGeneralProductInfoService/getGoldPriceInfo",
]


def _krx_items(url, key, rows):
    params = {
        "serviceKey": key,
        "resultType": "json",
        "numOfRows": rows,
        "pageNo": 1,
        "beginBasDt": (dt.datetime.now(KST) - dt.timedelta(days=365)).strftime("%Y%m%d"),
    }
    r = requests.get(url, params=params, timeout=TIMEOUT)
    try:
        j = r.json()
    except ValueError:
        # 인증 오류 등은 JSON이 아닌 XML/텍스트로 오는 경우가 많다
        raise RuntimeError(f"HTTP {r.status_code}, JSON 아님: {r.text[:150]!r}")
    if "OpenAPI_ServiceResponse" in j:  # 키/주소 오류는 이 형태로 온다
        h = j["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
        raise RuntimeError(f"{h.get('errMsg')} / {h.get('returnAuthMsg')} (코드 {h.get('returnReasonCode')})")
    resp = j.get("response", j)
    header = resp.get("header", {}) if isinstance(resp, dict) else {}
    code = str(header.get("resultCode", "00"))
    if code != "00":
        raise RuntimeError(f"API 오류 {code}: {header.get('resultMsg')}")
    items = (resp.get("body") or {}).get("items") or {}
    items = items.get("item", []) if isinstance(items, dict) else items
    if isinstance(items, dict):  # 결과가 1건이면 리스트가 아니라 dict로 온다
        items = [items]
    items = [i for i in items if "99.99_1kg" in str(i.get("itmsNm", "")).lower()]
    if not items:
        raise RuntimeError("금 99.99_1Kg 데이터 없음 (승인 대기 중이거나 종목명 불일치)")
    return items


def fetch_krx_gold():
    """공공데이터포털 금융위원회 일반상품시세정보 - 금 99.99_1Kg 종가(원/g).
    제공 데이터는 실시간이 아니며 영업일 하루 뒤 오후 1시 이후 갱신된다."""
    key = os.getenv("DATA_GO_KR_KEY")
    if not key:
        raise RuntimeError("DATA_GO_KR_KEY 없음")
    errors = []
    for url in KRX_GOLD_URLS:
        for rows in (800, 100):  # 한 번에 많이 못 받는 경우를 대비해 줄여서 재시도
            try:
                items = _krx_items(url, key, rows)
                break
            except Exception as e:
                errors.append(f"{url.split('/')[-1]}/{rows}: {e}")
                items = None
        if items:
            break
    if not items:
        raise RuntimeError(" | ".join(errors)[:300])
    items = sorted(items, key=lambda x: x["basDt"], reverse=True)
    value = float(items[0]["clpr"])
    prev = value - float(items[0].get("vs", 0))
    d = dt.datetime.strptime(items[0]["basDt"], "%Y%m%d").date()
    closes = [float(i["clpr"]) for i in items]
    return {"value": value, "prev": prev, "date": d, "status": "종가",
            "lo": min(closes), "hi": max(closes)}


def fmt(name, d, decimals):
    chg = (d["value"] / d["prev"] - 1) * 100 if d["prev"] else 0.0
    pos = None
    if d.get("lo") is not None and d["hi"] > d["lo"]:
        pos = (d["value"] - d["lo"]) / (d["hi"] - d["lo"]) * 100
    line = (f"{name}: {d['value']:,.{decimals}f} ({chg:+.2f}%) "
            f"[{d['status']} {d['date']:%m/%d}]")
    if pos is not None:
        line += f" 52주 {pos:.0f}%"
    return line, chg, pos


def collect():
    sections, changes, records = {}, {}, []

    def add(section, name, fetcher, decimals):
        try:
            d = fetcher()
            line, chg, pos = fmt(name, d, decimals)
            changes[name] = chg
            records.append({"section": section, "name": name, "value": d["value"],
                            "prev": d["prev"], "chg_pct": chg, "date": str(d["date"]),
                            "status": d["status"], "lo": d.get("lo"), "hi": d.get("hi"),
                            "pos": pos, "decimals": decimals})
        except Exception as e:  # 한 항목 실패가 전체를 막지 않도록
            msg = f"{type(e).__name__}: {str(e)[:200]}"
            line = f"{name}: 조회 실패 ({msg})"
            records.append({"section": section, "name": name, "error": msg})
        sections.setdefault(section, []).append(line)

    for section, name, ticker, dec in YAHOO_ITEMS:
        add(section, name, lambda t=ticker: fetch_yahoo(t), dec)
        if name == "미 10년물(%)":
            add(section, "미 2년물(%)", fetch_treasury_2y, 3)
    add("원자재", "KRX 금(원/g)", fetch_krx_gold, 0)
    return sections, changes, records


def rule_based_points(ch):
    pts = []
    sp, nq = ch.get("S&P500"), ch.get("나스닥 종합")
    if sp is not None and nq is not None:
        pts.append(f"미 증시: S&P500 {sp:+.2f}%, 나스닥 {nq:+.2f}%로 "
                   + ("기술주가 상대적으로 약했습니다." if nq < sp else "기술주가 시장을 앞섰습니다."))
    if "미 10년물(%)" in ch:
        pts.append(f"금리: 10년물 전일比 {ch['미 10년물(%)']:+.2f}% 변동, 달러인덱스 {ch.get('달러 인덱스', 0):+.2f}%.")
    if "WTI($)" in ch or "국제 금($/oz)" in ch:
        pts.append(f"원자재: WTI {ch.get('WTI($)', 0):+.2f}%, 금 {ch.get('국제 금($/oz)', 0):+.2f}%.")
    if "코스피" in ch or "원/달러" in ch:
        pts.append(f"한국: 코스피 {ch.get('코스피', 0):+.2f}%, 원/달러 {ch.get('원/달러', 0):+.2f}%.")
    if "VIX" in ch:
        pts.append(f"변동성: VIX 전일比 {ch['VIX']:+.2f}%.")
    return "\n".join(f"{i}. {p}" for i, p in enumerate(pts[:5], 1))


def claude_points(body, fallback):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return fallback
    prompt = (
        "아래는 오늘 아침 시장 수치다. 시장 흐름을 한국어로 3~5개 핵심 포인트로 "
        "간결하게 해석해 줘. 번호 목록만 출력하고, 수치에 없는 사실은 지어내지 마.\n\n" + body
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-5-5", "max_tokens": 600,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except Exception:
        return fallback


def build_message():
    now = dt.datetime.now(KST)
    sections, changes, records = collect()
    parts = [f"시장 브리핑 {now:%Y-%m-%d %H:%M} KST"]
    for title, lines in sections.items():
        parts.append(f"\n[{title}]\n" + "\n".join(lines))
    body = "\n".join(parts)
    points = claude_points(body, rule_based_points(changes))
    return body + "\n\n[핵심 포인트]\n" + points, records, points


def push(text):
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        raise SystemExit("NTFY_TOPIC 환경변수를 설정하세요.")
    data = text.encode("utf-8")[:3900]  # ntfy 본문 한도(4096B) 여유
    r = requests.post(
        f"https://ntfy.sh/{topic}",
        data=data,
        headers={"Title": "Morning Market Briefing", "Tags": "chart_with_upwards_trend"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()


def save_json(records, points, path="docs/data.json"):
    """대시보드 웹페이지가 읽을 데이터 파일."""
    import json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"generated_kst": dt.datetime.now(KST).isoformat(timespec="minutes"),
                   "points": points, "items": records}, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    msg, records, points = build_message()
    print(msg)
    save_json(records, points)
    if "--dry-run" not in sys.argv:
        push(msg)
        print("\n푸시 전송 완료")
