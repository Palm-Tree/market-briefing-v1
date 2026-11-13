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
    ("주가지수", "TIGER 미국S&P500", "360750.KS", 0),
    ("주가지수", "KODEX 미국S&P500", "379800.KS", 0),
    ("주가지수", "TIGER 미국나스닥100", "133690.KS", 0),
    ("주가지수", "KODEX 미국나스닥100", "379810.KS", 0),
    ("주가지수", "VIX", "^VIX", 2),
    ("환율·금리", "원/달러", "KRW=X", 2),
    ("환율·금리", "원/엔(100엔)", "JPYKRW=X", 2),
    ("환율·금리", "달러 인덱스", "DX-Y.NYB", 2),
    ("환율·금리", "미 10년물(%)", "^TNX", 3),
    ("원자재", "WTI($)", "CL=F", 2),
    ("원자재", "국제 금($/oz)", "GC=F", 2),
]


SCALE = {"JPYKRW=X": 100}  # 야후는 1엔당 원화 -> 100엔 기준으로 환산


def fetch_yahoo(ticker):
    """1년치 일봉으로 현재가/전일比/52주 범위 계산.
    장중이면 당일 봉(지연 시세)이 마지막 행. 52주 범위는 매 실행마다 최근 1년으로 다시 계산되고,
    현재가가 기존 최고/최저를 넘으면 범위도 함께 갱신된다."""
    h = yf.Ticker(ticker).history(period="1y", interval="1d", auto_adjust=False)
    h = h.dropna(subset=["Close"])
    last, prev = h.iloc[-1], h.iloc[-2]
    value, prev_v = float(last["Close"]), float(prev["Close"])
    lo, hi = float(h["Low"].min()), float(h["High"].max())
    k = SCALE.get(ticker, 1)
    value, prev_v, lo, hi = value * k, prev_v * k, lo * k, hi * k
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


FG_KO = {"extreme fear": "극단적 공포", "fear": "공포", "neutral": "중립",
         "greed": "탐욕", "extreme greed": "극단적 탐욕"}


def fetch_fear_greed():
    """CNN 공포탐욕지수(0~100). 비공식 주소라 차단되거나 바뀔 수 있다.
    52주 범위는 CNN이 주는 과거 데이터에서 최근 1년치로 계산."""
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
               "Accept": "application/json", "Referer": "https://edition.cnn.com/"}
    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    fg = j["fear_and_greed"]
    value, prev = float(fg["score"]), float(fg["previous_close"])
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=365)).timestamp() * 1000
    ys = [float(p["y"]) for p in j.get("fear_and_greed_historical", {}).get("data", [])
          if p.get("x", 0) >= cutoff]
    lo, hi = (min(ys + [value]), max(ys + [value])) if ys else (0.0, 100.0)
    try:
        d = dt.datetime.fromisoformat(str(fg["timestamp"]).replace("Z", "+00:00")).date()
    except Exception:
        d = dt.date.today()
    rating = str(fg.get("rating", "")).lower()
    return {"value": value, "prev": prev, "date": d, "status": "지수", "lo": lo, "hi": hi,
            "extra": FG_KO.get(rating, rating)}


_ECOS_ITEMS = {}


def _ecos(path):
    r = requests.get("https://ecos.bok.or.kr/api/" + path, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if "RESULT" in j:  # 오류 응답 (인증키 오류, 데이터 없음 등)
        raise RuntimeError(f"ECOS {j['RESULT'].get('CODE')}: {j['RESULT'].get('MESSAGE')}")
    return j


def _ecos_item_code(key, tenor):
    """시장금리(일별, 817Y002) 항목 목록에서 '국고채(2년)' 같은 항목코드를 찾는다."""
    if not _ECOS_ITEMS:
        j = _ecos(f"StatisticItemList/{key}/json/kr/1/200/817Y002")
        for row in j["StatisticItemList"]["row"]:
            _ECOS_ITEMS.setdefault(row["ITEM_NAME"].replace(" ", ""), row["ITEM_CODE"])
    for name, code in _ECOS_ITEMS.items():
        if name.startswith("국고채") and f"({tenor})" in name:
            return code
    raise RuntimeError(f"ECOS 항목목록에서 국고채({tenor}) 없음")


def fetch_kr_yield(tenor):
    """한국 국고채 금리 (한국은행 ECOS 817Y002, 일별). tenor: '2년' 또는 '10년'.
    ECOS는 보통 하루 늦게 반영된다."""
    key = os.getenv("ECOS_API_KEY")
    if not key:
        raise RuntimeError("ECOS_API_KEY 없음")
    code = _ecos_item_code(key, tenor)
    today = dt.datetime.now(KST).date()
    start = (today - dt.timedelta(days=400)).strftime("%Y%m%d")
    j = _ecos(f"StatisticSearch/{key}/json/kr/1/600/817Y002/D/{start}/{today:%Y%m%d}/{code}")
    rows = []
    for row in j["StatisticSearch"]["row"]:
        try:
            rows.append((dt.datetime.strptime(row["TIME"], "%Y%m%d").date(), float(row["DATA_VALUE"])))
        except (ValueError, KeyError):
            continue
    rows.sort()
    cutoff = rows[-1][0] - dt.timedelta(days=365)
    window = [v for d, v in rows if d >= cutoff]
    return {"value": rows[-1][1], "prev": rows[-2][1], "date": rows[-1][0], "status": "종가",
            "lo": min(window), "hi": max(window)}


def fmt(name, d, decimals):
    chg = (d["value"] / d["prev"] - 1) * 100 if d["prev"] else 0.0
    pos = None
    if d.get("lo") is not None and d["hi"] > d["lo"]:
        pos = (d["value"] - d["lo"]) / (d["hi"] - d["lo"]) * 100
    line = (f"{name}: {d['value']:,.{decimals}f} ({chg:+.2f}%) "
            f"[{d['status']} {d['date']:%m/%d}]")
    if d.get("extra"):
        line += f" {d['extra']}"
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
                            "pos": pos, "decimals": decimals, "extra": d.get("extra")})
        except Exception as e:  # 한 항목 실패가 전체를 막지 않도록
            msg = f"{type(e).__name__}: {str(e)[:200]}"
            line = f"{name}: 조회 실패 ({msg})"
            records.append({"section": section, "name": name, "error": msg})
        sections.setdefault(section, []).append(line)

    for section, name, ticker, dec in YAHOO_ITEMS:
        add(section, name, lambda t=ticker: fetch_yahoo(t), dec)
        if name == "VIX":
            add(section, "공포탐욕지수", fetch_fear_greed, 0)
        if name == "미 10년물(%)":
            add(section, "미 2년물(%)", fetch_treasury_2y, 3)
            add(section, "한국 10년물(%)", lambda: fetch_kr_yield("10년"), 3)
            add(section, "한국 2년물(%)", lambda: fetch_kr_yield("2년"), 3)
    add("원자재", "KRX 금(원/g)", fetch_krx_gold, 0)
    return sections, changes, records


def rule_based_points(records):
    """Claude API 키가 없을 때 쓰는 규칙 기반 요약 (최대 5개). 새 항목도 모두 포함."""
    R = {r["name"]: r for r in records if "error" not in r}

    def pct(n):
        return f"{R[n]['chg_pct']:+.2f}%"

    def bp(n):
        return f"{(R[n]['value'] - R[n]['prev']) * 100:+.0f}bp"

    pts = []
    if "S&P500" in R and "나스닥 종합" in R:
        t = f"미 증시: S&P500 {pct('S&P500')}, 나스닥 {pct('나스닥 종합')}로 " + (
            "기술주가 상대적으로 약했습니다." if R["나스닥 종합"]["chg_pct"] < R["S&P500"]["chg_pct"]
            else "기술주가 시장을 앞섰습니다.")
        etfs = [n for n in ("TIGER 미국S&P500", "KODEX 미국S&P500",
                            "TIGER 미국나스닥100", "KODEX 미국나스닥100") if n in R]
        if etfs:
            t += " 국내 상장 미국지수 ETF는 " + ", ".join(
                f"{n.replace('미국', '')} {pct(n)}" for n in etfs) + "입니다."
        pts.append(t)
    elif "코스피" in R:
        pts.append(f"코스피 {pct('코스피')}.")
    rates = []
    for n, label in (("미 10년물(%)", "미 10년"), ("미 2년물(%)", "미 2년"),
                     ("한국 10년물(%)", "한국 10년"), ("한국 2년물(%)", "한국 2년")):
        if n in R:
            rates.append(f"{label} {R[n]['value']:.2f}%({bp(n)})")
    if rates:
        t = "금리: " + ", ".join(rates) + "."
        if "미 10년물(%)" in R and "미 2년물(%)" in R:
            t += f" 미 장단기차(10Y-2Y) {R['미 10년물(%)']['value'] - R['미 2년물(%)']['value']:+.2f}%p."
        if "한국 10년물(%)" in R and "한국 2년물(%)" in R:
            t += f" 한국 장단기차 {R['한국 10년물(%)']['value'] - R['한국 2년물(%)']['value']:+.2f}%p."
        pts.append(t)
    mood = []
    if "공포탐욕지수" in R:
        fg = R["공포탐욕지수"]
        mood.append(f"공포탐욕지수 {fg['value']:.0f}({fg.get('extra') or '-'}, "
                    f"전일比 {fg['value'] - fg['prev']:+.0f}pt)")
    if "VIX" in R:
        mood.append(f"VIX {R['VIX']['value']:.2f}({pct('VIX')})")
    if mood:
        pts.append("심리: " + ", ".join(mood) + ".")
    com = [f"{label} {pct(n)}" for n, label in (("WTI($)", "WTI"), ("국제 금($/oz)", "국제 금"),
                                                ("KRX 금(원/g)", "KRX 금")) if n in R]
    if com:
        pts.append("원자재: " + ", ".join(com) + ".")
    kr = [f"{label} {pct(n)}" for n, label in (("코스피", "코스피"), ("원/달러", "원/달러"),
                                              ("원/엔(100엔)", "원/엔"), ("달러 인덱스", "달러인덱스")) if n in R]
    if kr:
        pts.append("한국·환율: " + ", ".join(kr) + ".")
    return "\n".join(f"{i}. {p}" for i, p in enumerate(pts[:5], 1))


def claude_points(body, fallback):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return fallback
    prompt = (
        "아래는 오늘 아침 시장 수치다. 시장 흐름을 한국어로 3~5개 핵심 포인트로 "
        "간결하게 해석해 줘. 번호 목록만 출력하고, 수치에 없는 사실은 지어내지 마.\n"
        "다음 항목은 수치가 있으면 반드시 핵심 포인트 안에 녹여서 언급해 줘: "
        "(1) 국내 상장 미국지수 ETF 4종(TIGER·KODEX 미국S&P500, 미국나스닥100)의 움직임과 "
        "해당 미국 지수와의 관계, (2) 공포탐욕지수(VIX와 함께 투자심리), "
        "(3) 미국·한국 국채 10년물·2년물 금리와 장단기 금리차. "
        "'조회 실패'로 표시된 항목은 언급하지 마.\n\n" + body
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-5-5", "max_tokens": 900,
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
    points = claude_points(body, rule_based_points(records))
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
