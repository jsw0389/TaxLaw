#!/usr/bin/env python3
"""세법 판례·심판례·해석례·행정규칙을 받아 refs/ 에 한 건씩 텍스트로 저장한다.

법령 본문(laws/, fetch_laws.py)의 짝이다. 받는 자료와 출처:

| 폴더 | 자료 | 목록 | 본문 |
|---|---|---|---|
| 판례 | 세무 판례(대법원·하급심) | 법제처 prec | 법제처, 없으면 국세법령정보시스템 |
| 헌재결정례 | 세법 관련 헌재 결정 | 법제처 detc | 법제처 |
| 조세심판원 | 조세심판원 재결례 | 법제처 ttSpecialDecc | 법제처 |
| 해석_법제처 | 법제처 법령해석례(세법) | 법제처 expc | 법제처 |
| 해석_국세청 | 국세청 질의회신 | 법제처 ntsCgmExpc | 국세법령정보시스템 |
| 해석_기재부 | 기획재정부(재정경제부) 해석 | 법제처 moefCgmExpc | 국세법령정보시스템 |
| 해석_행안부 | 행정안전부 지방세 해석 | 법제처 moisCgmExpc | 법제처 |
| 행정규칙 | 국세청·기재부·조세심판원·행안부(지방세) 현행 훈령·예규·고시(기본통칙 등) | 법제처 admrul | 법제처 |

국세청·기재부 해석과 국세청 출처 판례는 법제처 Open API가 목록만 주고 본문을 안 준다.
그래서 본문은 국세법령정보시스템(taxlaw.nts.go.kr) 공개 화면이 쓰는 주소에서 받는다.

동작 원칙
- 목록은 매번 처음부터 끝까지 훑고(목록 호출은 싸다), 아직 없는 건만 본문을 받는다.
  같은 건은 다시 받지 않는다(판례·해석은 한 번 나오면 바뀌지 않는다). 행정규칙만 현행본이 바뀌면 다시 받는다.
- 세법 자료만 고르는 일은 목록 항목의 칸(사건종류명·안건명·소관부처명)으로 여기서 한다.
  API의 검색 조건 파라미터에 기대지 않는다 — 이름이 틀리면 조용히 무시되고 전체가 오기 때문이다.
- 처음에는 몇십만 건이라 한 번에 못 받는다. REFS_BUDGET_MIN(분)이 지나면 멈추고 저장한 만큼 커밋한다.
  워크플로가 남은 게 있으면 자신을 다시 띄워 이어 받는다(.github/workflows/fetch-refs.yml).
- 최근 것부터 받고, 자료 종류를 번갈아 받는다(한 종류가 며칠씩 다른 것을 막지 않게).
- 한 건이 실패하면 다음 실행에 다시 시도한다. 세 번 실패하면 건너뛰고 refs/_state.json 에 남긴다.
- 실행일 같은 매번 바뀌는 값은 파일에 쓰지 않는다. 받은 게 있는 실행에만 커밋이 생긴다.

환경변수
- LAW_OC: open.law.go.kr 인증키(OC). 필수
- REFS_BUDGET_MIN: 이번 실행에서 쓸 시간(분). 기본 200
- REFS_ONLY: 쉼표로 고른 폴더만 받기(예: "판례,조세심판원"). 시험용
- REFS_LIMIT: 자료 종류마다 받을 최대 건수. 시험용
"""
import datetime
import html
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "refs"
STATE = OUT / "_state.json"
CATALOG = OUT / "_catalog"   # 자료 종류마다 고른 목록(JSONL). 목록을 매번 처음부터 훑지 않으려고 둔다
# 주소를 바꾸는 환경변수는 모의 서버로 시험할 때만 쓴다
BASE = os.environ.get("REFS_BASE", "https://www.law.go.kr/DRF")
TAXLAW = os.environ.get("REFS_TAXLAW", "https://taxlaw.nts.go.kr")
OC = os.environ.get("LAW_OC", "").strip()
BUDGET = float(os.environ.get("REFS_BUDGET_MIN", "200")) * 60
ONLY = {s.strip() for s in os.environ.get("REFS_ONLY", "").split(",") if s.strip()}
LIMIT = int(os.environ.get("REFS_LIMIT", "0") or 0)
WORKERS = 8          # 동시 요청 수. 한 번 다녀오는 데 4~5초 걸려서(2026-11-11 실측) 나란히 보내야 한다
LIST_SHARE = 0.5     # 시간 예산 가운데 목록 훑기에 쓸 수 있는 몫. 나머지는 본문 받기에 남긴다
MAX_FAILS = 3        # 이만큼 실패한 건은 건너뛴다
PAGE = 100           # 목록 한 쪽 건수(API 최대)
STARTED = time.monotonic()

# 세법 이름·세목 낱말. 안건명·사건명으로 세법 자료를 고를 때 쓴다.
TAX_WORDS = (
    "국세", "소득세", "법인세", "부가가치세", "부가세", "상속세", "증여세", "조세", "종합부동산세", "종부세",
    "개별소비세", "교통ㆍ에너지ㆍ환경세", "교통·에너지·환경세", "주세", "인지세", "증권거래세", "교육세",
    "농어촌특별세", "지방세", "취득세", "재산세", "등록면허세", "지방소득세", "주민세", "자동차세",
    "레저세", "담배소비세", "지역자원시설세", "지방소비세", "세무사", "과세", "납세", "세법", "세액", "세율",
    "원천징수", "가산세", "양도소득",
)
TAX_RE = re.compile("|".join(map(re.escape, TAX_WORDS)))
LOCAL_RE = re.compile(
    "지방세|취득세|재산세|등록면허세|지방소득세|주민세|자동차세|지방교육세|레저세|담배소비세|"
    "지역자원시설세|지방소비세|과세|납세|세율|세액|세무|감면"
)
TAX_ORGS = {"국세청", "조세심판원"}
FINANCE_ORGS = {"기획재정부", "재정경제부"}   # 2025년 조직개편 전후 이름 둘 다


class FetchError(Exception):
    """한 건(또는 목록 한 쪽)을 받지 못함. 그것만 건너뛰고 계속한다."""


class OutOfTime(Exception):
    """시간 예산을 다 씀."""


def over_budget():
    return time.monotonic() - STARTED > BUDGET


# ---------------------------------------------------------------- HTTP

class Host:
    """호스트마다 요청 간격을 지키고, 연달아 실패하면 이번 실행에서는 그 호스트를 쉰다."""

    def __init__(self, name, interval):
        self.name = name
        self.interval = interval
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.streak = 0
        self.dead = False

    def wait(self):
        if self.dead:
            raise FetchError(f"{self.name}: 연속 실패가 많아 이번 실행에서는 쉼")
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.interval
        time.sleep(max(0.0, at - now))

    def result(self, ok):
        with self.lock:
            self.streak = 0 if ok else self.streak + 1
            if self.streak >= 30 and not self.dead:
                self.dead = True
                print(f"::warning::{self.name} 연속 30회 실패 — 이번 실행에서 이 호스트는 중단")


GAP = float(os.environ.get("REFS_GAP_SCALE", "1"))   # 시험용: 요청 간격 배율
LAW_HOST = Host("law.go.kr", 0.25 * GAP)
TAX_HOST = Host("taxlaw.nts.go.kr", 0.5 * GAP)
TAX_HOSTNAME = urllib.parse.urlparse(TAXLAW).hostname
OK_HOSTNAMES = {urllib.parse.urlparse(BASE).hostname, TAX_HOSTNAME}
UA = "Mozilla/5.0 (jtax-blog refs fetch)"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


NO_REDIRECT = urllib.request.build_opener(NoRedirect)


def http(host, url, data=None, headers=None, label="", follow=True):
    """요청 하나. 두 번까지 다시 시도. (본문, 최종 주소, Location 헤더)를 돌려준다.
    인증키가 로그에 찍히지 않도록 URL은 출력하지 않는다."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    last = None
    for attempt in range(3):
        host.wait()
        try:
            opener = urllib.request.urlopen if follow else NO_REDIRECT.open
            with opener(req, timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
                host.result(True)
                return body, r.geturl(), r.headers.get("Location")
        except urllib.error.HTTPError as e:
            if not follow and 300 <= e.code < 400:   # 따라가지 않기로 한 넘김
                host.result(True)
                return "", url, e.headers.get("Location")
            last = f"HTTP {e.code}"
            if e.code in (400, 404):
                break
        except Exception as e:
            last = str(e)
        host.result(False)
        time.sleep(5 * (attempt + 1))
    raise FetchError(f"{host.name} {label} 접속 실패: {last}")


def law_url(endpoint, **params):
    return f"{BASE}/{endpoint}?" + urllib.parse.urlencode({"OC": OC, **params})


def law_list_page(target, page, **extra):
    """목록 한 쪽(XML). (전체 건수, 항목 목록[dict])"""
    body, _, _ = http(LAW_HOST, law_url("lawSearch.do", target=target, type="XML",
                                        display=PAGE, page=page, **extra), label=f"{target} 목록 {page}쪽")
    try:
        root = ET.fromstring(body.strip())
    except ET.ParseError:
        raise FetchError(f"{target} 목록 {page}쪽 응답이 XML이 아님. 앞부분:\n{body[:500]}")
    total = int((root.findtext("totalCnt") or "0").strip() or 0)
    items = []
    for el in root:
        if len(el):   # 자식이 있는 요소가 항목이다(totalCnt·page 같은 잎은 건너뜀)
            items.append({c.tag: (c.text or "").strip() for c in el})
    return total, items


def law_detail(target, id_):
    """본문(JSON). 맨 위 감싸개 하나를 벗긴 dict를 돌려준다."""
    body, _, _ = http(LAW_HOST, law_url("lawService.do", target=target, type="JSON", ID=id_),
                      label=f"{target} {id_}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return law_detail_xml(target, id_, body)
    if isinstance(data, dict) and len(data) == 1:
        data = next(iter(data.values()))
    if not isinstance(data, dict) or len(data) < 3:
        raise FetchError(f"{target} {id_} 본문 없음: {str(data)[:200]}")
    return data


def law_detail_xml(target, id_, json_body):
    """JSON을 안 주는 자료가 있으면 XML로 한 번 더 받는다. 잎 요소를 (칸 이름, 글) 목록으로."""
    body, _, _ = http(LAW_HOST, law_url("lawService.do", target=target, type="XML", ID=id_),
                      label=f"{target} {id_} XML")
    try:
        root = ET.fromstring(body.strip())
    except ET.ParseError:
        raise FetchError(f"{target} {id_} 응답이 JSON도 XML도 아님. 앞부분: {json_body[:300]}")
    pairs = [[el.tag, el.text] for el in root.iter() if len(el) == 0 and (el.text or "").strip()]
    if len(pairs) < 3:
        raise FetchError(f"{target} {id_} 본문 없음(XML)")
    return pairs


def taxlaw_doc(ntst_id):
    """국세법령정보시스템 문서 하나(dcmDVO, 편집기 본문 목록)."""
    referer = f"{TAXLAW}/qt/USEQTA002P.do?ntstDcmId={ntst_id}"
    data = urllib.parse.urlencode({
        "actionId": "ASIQTB002PR01",
        "paramData": json.dumps({"dcmDVO": {"ntstDcmId": ntst_id}}),
    }).encode()
    body, _, _ = http(TAX_HOST, f"{TAXLAW}/action.do", data=data, label=f"ntstDcmId {ntst_id}", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": TAXLAW, "Referer": referer, "X-Requested-With": "XMLHttpRequest",
    })
    try:
        action = json.loads(body)["data"]["ASIQTB002PR01"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise FetchError(f"ntstDcmId {ntst_id} 응답 형식이 예상과 다름: {body[:300]}")
    dcm = action.get("dcmDVO") if isinstance(action, dict) else None
    if not dcm:
        raise FetchError(f"ntstDcmId {ntst_id} 문서 없음")
    editors = [e.get("dcmFleByte") for e in (action.get("dcmHwpEditorDVOList") or [])
               if isinstance(e, dict) and isinstance(e.get("dcmFleByte"), str)]
    return dcm, editors, referer


NTST_RE = re.compile(r"ntstDcmId=(\d{10,})")


def html_view(target, id_):
    """법제처 상세 화면(HTML). 국세청 자료면 (ntstDcmId, ""), 아니면 (None, 화면 글).
    화면 → iframe → 넘김을 따라가 국세법령정보시스템 문서번호(ntstDcmId)를 찾는다."""
    page, _, _ = http(LAW_HOST, law_url("lawService.do", target=target, type="HTML", ID=id_),
                      label=f"{target} {id_} 화면")
    m = NTST_RE.search(page)
    if m:
        return m.group(1), ""
    m = re.search(r"<iframe\b[^>]*\bsrc\s*=\s*[\"']?\s*([^\"'>\s]+)", page, re.I)
    if not m:
        return None, clean(page)
    prec_id = f"{target} {id_}"
    url = urllib.parse.urljoin("https://www.law.go.kr", html.unescape(m.group(1)))
    for _ in range(4):
        m = NTST_RE.search(url)
        if m:
            return m.group(1), ""
        host = urllib.parse.urlparse(url).hostname or ""
        if not (host.endswith("law.go.kr") or host.endswith("nts.go.kr") or host in OK_HOSTNAMES):
            raise FetchError(f"{prec_id} 넘김이 다른 곳으로 감: {host}")
        is_tax = host.endswith("nts.go.kr") or host == TAX_HOSTNAME
        body, _, loc = http(TAX_HOST if is_tax else LAW_HOST, url,
                            follow=False, label=f"{prec_id} 넘김")
        m = NTST_RE.search(body)
        if m:
            return m.group(1), ""
        if not loc:
            break
        url = urllib.parse.urljoin(url, loc)
    raise FetchError(f"{prec_id} 국세법령정보시스템 문서번호를 못 찾음")


# ---------------------------------------------------------------- 글 다듬기

def clean(s):
    """HTML 조각·<br/>이 섞인 글을 평문으로."""
    if s is None:
        return ""
    s = str(s)
    if "<" in s or "&" in s:
        s = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", s)
        s = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", s)
        s = re.sub(r"(?i)</\s*(p|div|tr|li|h\d|table|ul|ol)\s*>", "\n", s)
        s = re.sub(r"(?i)</\s*t[dh]\s*>", "\t", s)
        s = re.sub(r"<[^>]+>", "", s)
        s = html.unescape(s)
    out, blank = [], False
    for line in s.replace("\r", "").split("\n"):
        line = re.sub(r"[ \t 　]+", " ", line).strip()
        if line:
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def flatten(obj, key=""):
    """중첩된 JSON을 (칸 이름, 글) 차례로 편다. 칸 이름을 미리 몰라도 빠짐없이 싣기 위함."""
    if obj is None:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from flatten(v, k)
    elif isinstance(obj, list):
        if all(not isinstance(x, (dict, list)) for x in obj):
            t = "\n".join(c for c in (clean(x) for x in obj) if c)
            if t:
                yield key, t
        else:
            for x in obj:
                yield from flatten(x, key)
    else:
        t = clean(obj)
        if t:
            yield key, t


def pairs_of(data):
    """law_detail 결과(JSON dict 또는 XML 잎 목록)를 (칸 이름, 글) 목록으로."""
    if isinstance(data, list):
        return [(k, clean(v)) for k, v in data if clean(v)]
    return list(flatten(data))


SKIP_KEYS = re.compile(r"링크|Link|^OC$")


LONG_KEYS = {"조문내용", "부칙내용", "별표내용", "본문", "회신", "회답", "이유", "주문"}


def render_fields(pairs):
    """API가 준 차례 그대로 싣는다(조문 차례가 바뀌면 안 된다).
    짧은 칸은 `이름: 값` 한 줄, 긴 칸은 【이름】 제목 아래. 같은 이름이 이어지면 제목은 한 번만."""
    lines, prev = [], None
    for k, t in pairs:
        if SKIP_KEYS.search(k or ""):
            continue
        if len(t) <= 80 and "\n" not in t and k not in LONG_KEYS:
            lines.append(f"{k}: {t}")
            prev = None
        else:
            lines += [""] + ([] if k == prev else [f"【{k}】"]) + [t]
            prev = k
    return lines


def render_taxlaw(dcm, editors):
    pairs = [
        ("제목", dcm.get("ntstDcmTtl")), ("문서번호", dcm.get("ntstDcmDscmCntn")),
        ("사건번호", dcm.get("ntstPrdgHpnnNoCntn")), ("회신번호", dcm.get("ntstDcmRplyCntn")),
        ("생산일자", dcm.get("ntstDcmRgtDt")), ("기관", dcm.get("ogzNm")), ("유형", dcm.get("ntstDcmClNm")),
    ]
    pairs = [(k, clean(v)) for k, v in pairs if clean(v)]
    seen = set()
    for k, v in (("요지", dcm.get("ntstDcmGistCntn")), ("회신", dcm.get("ntstDcmCntn"))):
        t = clean(v)
        if t and t not in seen:
            pairs.append((k, t))
            seen.add(t)
    for e in editors:
        t = clean(e)
        if len(re.sub(r"\s", "", t)) >= 20 and t not in seen:
            pairs.append(("본문", t))
            seen.add(t)
    if not seen:
        raise FetchError(f"{dcm.get('ntstDcmId')} 본문이 비어 있음")
    return render_fields(pairs)


# ---------------------------------------------------------------- 자료 종류

def first(item, fields):
    for f in fields:
        v = (item.get(f) or "").strip()
        if v:
            return v
    return ""


class Source:
    def __init__(self, folder, target, id_field, date_fields, no_fields, title_fields,
                 keep=None, body="law", current=False, need=()):
        self.folder, self.target, self.id_field = folder, target, id_field
        self.date_fields, self.no_fields, self.title_fields = date_fields, no_fields, title_fields
        self.keep = keep or (lambda item: True)
        self.body = body          # law | taxlaw | prec
        self.current = current    # True: 현행본만 두고 바뀌면 다시 받는다(행정규칙)
        self.need = need          # 고르기에 쓰는 칸. 목록에 없으면 경고한다
        self.dir = OUT / folder

    def date(self, item):
        d = re.sub(r"\D", "", first(item, self.date_fields))
        if len(d) >= 8:
            return d[:8]
        # 조세심판원은 결정일자를 안 준다 → 청구번호(국심1997광0600·조심2023서1234)의 연도로 대신(월일 0000)
        m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", first(item, self.no_fields))
        return f"{m.group(1)}0000" if m else ""

    def path(self, item):
        id_ = item[self.id_field]
        if self.current:
            org = safe(item.get("소관부처명") or "기타", 20)
            return self.dir / org / f"{safe(first(item, self.title_fields), 60)}_{id_}.txt"   # 한글 60자=180바이트, 파일 이름 255바이트 한도 안
        d = self.date(item)
        no = safe(first(item, self.no_fields) or "번호없음", 40)
        return self.dir / (d[:4] or "연도미상") / f"{d or '00000000'}_{no}_{id_}.txt"


def safe(s, n):
    s = re.sub(r'[\\/:*?"<>|\s,]+', "_", s).strip("_.") or "_"
    return s[:n]


def keep_prec(item):
    return "세무" in item.get("사건종류명", "") or "국세" in item.get("데이터출처명", "")


def keep_title(*fields):
    return lambda item: any(TAX_RE.search(item.get(f, "")) for f in fields)


def keep_mois(item):
    return bool(LOCAL_RE.search(item.get("안건명", "")))


def keep_admrul(item):
    org, name = item.get("소관부처명", ""), item.get("행정규칙명", "")
    if item.get("현행연혁구분") and item["현행연혁구분"] != "현행":
        return False
    if org in TAX_ORGS:
        return True
    if org in FINANCE_ORGS:
        return bool(TAX_RE.search(name)) or "세" in name
    if org == "행정안전부":
        return bool(LOCAL_RE.search(name))
    return False


SOURCES = [
    Source("판례", "prec", "판례일련번호", ("선고일자",), ("사건번호",), ("사건명",),
           keep=keep_prec, body="prec", need=("사건종류명",)),
    Source("헌재결정례", "detc", "헌재결정례일련번호", ("종국일자",), ("사건번호",), ("사건명",),
           keep=keep_title("사건명"), need=("사건명",)),
    Source("조세심판원", "ttSpecialDecc", "특별행정심판재결례일련번호", ("의결일자", "처분일자"),
           ("청구번호", "사건번호"), ("사건명",)),
    Source("해석_법제처", "expc", "법령해석례일련번호", ("회신일자", "해석일자"), ("안건번호",), ("안건명",),
           keep=keep_title("안건명"), need=("안건명",)),
    Source("해석_국세청", "ntsCgmExpc", "법령해석일련번호", ("해석일자",), ("안건번호",), ("안건명",),
           body="taxlaw"),
    Source("해석_기재부", "moefCgmExpc", "법령해석일련번호", ("해석일자",), ("안건번호",), ("안건명",),
           body="taxlaw"),
    Source("해석_행안부", "moisCgmExpc", "법령해석일련번호", ("해석일자", "등록일시"), ("안건번호",), ("안건명",),
           keep=keep_mois, need=("안건명",)),
    Source("행정규칙", "admrul", "행정규칙일련번호", ("발령일자", "시행일자"), ("발령번호",), ("행정규칙명",),
           keep=keep_admrul, current=True, need=("소관부처명", "행정규칙명")),
]


# ---------------------------------------------------------------- 목록
#
# 전체 목록이 크다(판례 17만 5천 건, 한 쪽 100건에 4~5초). 매번 처음부터 훑으면 시간이 다 간다.
# 그래서 고른 항목을 refs/_catalog/<자료>.jsonl 에 남기고,
# - 처음에는 오래된 것부터(sort=dasc) 끝까지 훑되, 시간이 모자라면 다음 실행에 이어서 훑는다.
# - 다 훑은 뒤에는 최신 것부터(sort=ddes) 저장된 가장 최근 날짜보다 60일 앞까지만 본다.
# 행정규칙은 현행본만 두므로 매번 처음부터 다시 훑는다(건수가 적다).

KEEP_KEYS = re.compile(r"일련번호|일자|번호|명$|링크")   # 목록에 남길 칸(본문 받기·파일 이름에 쓰는 것)


OC_RE = re.compile(r"([?&])OC=[^&]*&?")   # 법제처가 상세링크에 인증키(OC)를 넣어 준다. 공개 저장소라 절대 남기지 않는다


def no_oc(v):
    return OC_RE.sub(r"\1", v) if isinstance(v, str) else v


def slim(item):
    return {k: no_oc(v) for k, v in item.items() if v and KEEP_KEYS.search(k)}


def cat_path(src):
    return CATALOG / f"{src.folder}.jsonl"


def load_catalog(src):
    cat = {}
    p = cat_path(src)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                it = slim(json.loads(line))   # 옛 목록에 남은 인증키도 여기서 지워진다
                cat[it[src.id_field]] = it
    return cat


def save_catalog(src, cat):
    CATALOG.mkdir(parents=True, exist_ok=True)
    keys = sorted(cat, key=lambda k: (len(k), k))   # 일련번호 차례. 새 항목만 diff 에 생기게
    lines = [json.dumps(cat[k], ensure_ascii=False, sort_keys=True) for k in keys]
    cat_path(src).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def list_time_left():
    return STARTED + BUDGET * LIST_SHARE - time.monotonic()


def check_fields(src, items):
    for field in src.need:
        if items and not any(field in it for it in items):
            print(f"::warning::[{src.folder}] 목록에 '{field}' 칸이 없음 → 고르기가 안 됨. 있는 칸: {sorted(items[0])}")


def add_items(src, cat, items):
    n = 0
    for it in items:
        id_ = it.get(src.id_field)
        if id_ and id_ not in cat and src.keep(it):
            cat[id_] = slim(it)
            n += 1
    return n


def full_scan(src, cat, st):
    """오래된 것부터 끝까지. 다 못 하면 st['page']에 다음에 훑을 쪽을 남긴다. 새로 고른 건수를 돌려준다."""
    extra = {"sort": "dasc"}
    page = max(1, int(st.get("page", 1)))
    total, items = law_list_page(src.target, page, **extra)
    if not items:
        st.update(complete=True, page=page)
        return 0
    if page == 1:
        check_fields(src, items)
    per = int(st.get("per") or len(items))   # 한 쪽 건수. 이어 훑을 때 마지막 쪽이 짧아도 첫 쪽 값을 쓴다
    pages = max(1, math.ceil(total / per))
    added = add_items(src, cat, items)
    page += 1
    batch = WORKERS * 3
    while page <= pages and list_time_left() > 0:
        todo = list(range(page, min(pages, page + batch - 1) + 1))
        got = {}
        with ThreadPoolExecutor(WORKERS) as pool:
            futures = {pool.submit(law_list_page, src.target, p, **extra): p for p in todo}
            for f in as_completed(futures):
                try:
                    got[futures[f]] = f.result()[1]
                except FetchError as e:
                    print(f"::warning::[{src.folder}] {e}")
        for p in todo:            # 앞에서부터 이어진 쪽만 반영한다. 빠진 쪽이 있으면 거기서 다음에 다시
            if p not in got:
                break
            added += add_items(src, cat, got[p])
            page = p + 1
        if page <= todo[-1]:
            break
    st.update(page=page, pages=pages, total=total, per=per, complete=page > pages)
    if st["complete"]:
        st["full_done"] = today()
    print(f"[목록] {src.folder}: {min(page - 1, pages)}/{pages}쪽(전체 {total}건), 세법 자료 {len(cat)}건 (+{added})"
          + ("" if st["complete"] else " — 다음 실행에 이어 훑음"))
    return added


def today():
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)).strftime("%Y%m%d")


RESCAN_DAYS = 28   # 최신 쪽만 보면 날짜가 예전인데 늦게 등록된 건을 놓친다. 이만큼 지나면 처음부터 다시 훑는다


def days_before(yyyymmdd, n):
    try:
        d = datetime.date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8]))
    except ValueError:
        return ""
    return (d - datetime.timedelta(days=n)).strftime("%Y%m%d")


def new_scan(src, cat, st):
    """다 훑은 뒤: 최신 것부터. 저장된 가장 최근 날짜보다 60일 앞까지 내려가면 멈춘다(최대 50쪽)."""
    newest = max((src.date(it) for it in cat.values()), default="")
    stop = days_before(newest, 60) if newest else ""
    added, p = 0, 0
    for p in range(1, 51):
        _, items = law_list_page(src.target, p, sort="ddes")
        if not items:
            break
        dates = [src.date(it) for it in items if src.date(it)]
        rising = sum(1 for a, b in zip(dates, dates[1:]) if a < b)
        if p == 1 and len(dates) > 10 and rising > len(dates) // 10:
            # 정렬이 안 먹는다 → 최신 몇 쪽만 보는 방법을 못 쓴다. 다음부터 처음부터 다시 훑는다
            print(f"::warning::[{src.folder}] 최신순 정렬이 안 됨 → 다음 실행에 목록을 처음부터 다시 훑음")
            st.update(complete=False, page=1)
            break
        added += add_items(src, cat, items)
        if not stop or (dates and max(dates) < stop):
            break
    print(f"[목록] {src.folder}: 최신 {p}쪽 확인, 세법 자료 {len(cat)}건 (+{added})")
    return added


ID_RE = re.compile(r"_(\d+)\.txt$")


def stored(src):
    """이미 받은 파일: {ID: 경로}"""
    found = {}
    if src.dir.exists():
        for p in src.dir.rglob("*.txt"):
            m = ID_RE.search(p.name)
            if m:
                found[m.group(1)] = p
    return found


# ---------------------------------------------------------------- 본문

def body_size(pairs):
    return sum(len(t) for k, t in pairs if len(t) > 80 or k in LONG_KEYS)


THIN = 300          # 긴 칸 글자 수가 이보다 적으면 본문이 없다고 본다
_THIN_SEEN = set()  # 자료 종류마다 처음 한 번만 칸 이름을 로그에 남긴다(원인 찾기용)


def taxlaw_pairs(dcm):
    return {"날짜": clean(dcm.get("ntstDcmRgtDt")), "번호": clean(dcm.get("ntstDcmDscmCntn"))}


def fetch_one(src, item):
    """본문 한 건을 받아 파일로. 목록에 날짜·번호가 없으면 본문에서 채운다."""
    id_ = item[src.id_field]
    origin = "국가법령정보센터 Open API"
    meta = {}
    lines = None

    def from_taxlaw(ntst):
        dcm, editors, url = taxlaw_doc(ntst)
        meta.update(taxlaw_pairs(dcm))
        return render_taxlaw(dcm, editors), f"국세법령정보시스템 {url}"

    if src.body == "taxlaw":
        m = NTST_RE.search(" ".join(v for k, v in item.items() if "링크" in k))
        if m:
            lines, origin = from_taxlaw(m.group(1))
    if lines is None:
        try:
            pairs = pairs_of(law_detail(src.target, id_))
        except FetchError:
            if src.body != "prec":
                raise
            pairs = []
        for k, t in pairs:
            meta.setdefault(k, t)
        if body_size(pairs) >= THIN or src.current:   # 행정규칙은 짧은 것도 조문이 본문이다
            lines = render_fields(pairs)
        else:
            # 법제처 JSON에 본문이 없다 → 상세 화면(HTML)을 본다. 국세청 자료면 국세법령정보시스템으로
            if src.folder not in _THIN_SEEN:
                _THIN_SEEN.add(src.folder)
                print(f"[본문 얇음] {src.folder} {id_}: JSON 칸 {[k for k, _ in pairs]} → 화면(HTML)으로 다시 봄")
            ntst, page_text = html_view(src.target, id_)
            if ntst:
                lines, origin = from_taxlaw(ntst)
            else:
                lines = render_fields(pairs)
                if len(re.sub(r"\s", "", page_text)) >= THIN:
                    lines += ["", "【본문(상세 화면)】", page_text]
                    origin = "국가법령정보센터 상세 화면"
                else:
                    lines += ["", "※ 본문 없음: 법제처가 제목·기본정보만 제공(원문은 조세심판원 등 기관 누리집에서 확인)"]
    item = dict(item)
    for f in src.date_fields:
        if not src.date(item) and (meta.get(f) or meta.get("날짜")):
            item[f] = meta.get(f) or meta.get("날짜")
    for f in src.no_fields:
        if not first(item, src.no_fields) and (meta.get(f) or meta.get("번호")):
            item[f] = meta.get(f) or meta.get("번호")
    title = first(item, src.title_fields)
    head = " | ".join(x for x in (src.folder, first(item, src.no_fields), src.date(item), title) if x)
    text = "\n".join([f"{head} | ID {id_}", f"출처: {origin} (scripts/fetch_refs.py 로 자동 수집)", ""] + lines)
    text = OC_RE.sub(r"\1", text)
    path = src.path(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text.rstrip() + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


# ---------------------------------------------------------------- 실행

def load_state():
    """{"fails": {자료: {ID: 실패 횟수}}, "scan": {자료: {page, pages, total, complete}}}"""
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    if "fails" not in state and "scan" not in state:   # 2026-11-11 첫 판의 모양({자료: {ID: 횟수}})
        state = {"fails": state}
    state.setdefault("fails", {})
    state.setdefault("scan", {})
    return state


def save_state(state):
    out = {
        "fails": {k: dict(sorted(v.items())) for k, v in sorted(state["fails"].items()) if v},
        "scan": {k: dict(sorted(v.items())) for k, v in sorted(state["scan"].items()) if v},
    }
    STATE.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def interleave(queues):
    """자료 종류마다 최근 것부터 줄 세운 것을 한 건씩 번갈아 섞는다."""
    out, i = [], 0
    longest = max((len(q) for q in queues), default=0)
    while i < longest:
        for q in queues:
            if i < len(q):
                out.append(q[i])
        i += 1
    return out


def write_readme(summary):
    rows = [f"| {s['folder']} | {s['stored']} | {s['target']} | {s['left']} | {s['failed']} | {s['scan']} |"
            for s in summary]
    md = [
        "# 세법 판례·심판례·해석례·행정규칙 (자동 수집)",
        "",
        "scripts/fetch_refs.py 가 GitHub Actions(.github/workflows/fetch-refs.yml)로 받아 한 건에 파일 하나로 둔다.",
        "법령 본문은 ../laws/ 에 따로 있다. 답변·원고 근거를 찾을 때는 이 폴더를 grep 한다.",
        "",
        "- 파일 이름: `<연도>/<선고·의결·해석일자>_<사건·청구·안건번호>_<일련번호>.txt`",
        "  (행정규칙만 `<소관부처>/<행정규칙명>_<일련번호>.txt`, 현행본만 둔다)",
        "- 파일 첫 줄: `자료 | 번호 | 날짜 | 제목 | ID`. 둘째 줄: 출처",
        "- 판례는 법제처 분류가 '세무'이거나 국세청 출처인 것. 헌재·법제처 해석은 사건명·안건명에 세목 낱말이 든 것",
        "- 국세청·기재부 해석과 국세청 출처 판례 본문은 국세법령정보시스템(taxlaw.nts.go.kr)에서 받았다",
        "- `_catalog/` 는 고른 목록(아직 본문을 안 받은 것 포함), `_state.json` 은 이어 받기 상태다",
        "- 처음 모으는 동안은 '남음'이 0이 되고 목록을 다 훑을 때까지 워크플로가 스스로 이어 받는다",
        "",
        "| 자료 | 저장 | 대상(고른 목록) | 남음 | 3회 실패로 건너뜀 | 목록 훑기 |",
        "|---|---|---|---|---|---|",
    ] + rows
    (OUT / "README.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def update_catalog(src, cat, st):
    """목록을 갱신한다. 새로 고른 것이 있거나 훑은 쪽이 늘었으면 True."""
    if src.current:
        # 현행본: 매번 처음부터. 다 훑었을 때만 목록을 갈아 끼우고, 현행이 아니게 된 파일을 지운다
        fresh, s2 = {}, {}
        full_scan(src, fresh, s2)
        new = sum(1 for k in fresh if k not in cat)
        if s2.get("complete") and fresh:
            cat.clear()
            cat.update(fresh)
            for id_, path in stored(src).items():
                if id_ not in cat:
                    path.unlink()   # 폐지·개정으로 현행이 아니게 된 것. git 이력에는 남는다
            st.update(complete=True, page=1)
            return new > 0
        for k, v in fresh.items():
            cat.setdefault(k, v)
        return new > 0
    if st.get("complete") and st.get("full_done", "") < days_before(today(), RESCAN_DAYS):
        print(f"[목록] {src.folder}: 마지막으로 다 훑은 지 {RESCAN_DAYS}일이 지나 처음부터 다시 훑음")
        st.update(complete=False, page=1)
    before = st.get("page", 1)
    if st.get("complete"):
        return new_scan(src, cat, st) > 0
    added = full_scan(src, cat, st)
    return added > 0 or st.get("page", 1) != before


def main():
    if not OC:
        sys.exit("[오류] 환경변수 LAW_OC 가 비어 있음 (GitHub Secrets 에 LAW_OC 등록 필요)")
    OUT.mkdir(exist_ok=True)
    state = load_state()
    sources = [s for s in SOURCES if not ONLY or s.folder in ONLY]
    queues, cats, listed, progressed = [], {}, 0, False

    for src in sources:
        cat = load_catalog(src)
        st = state["scan"].setdefault(src.folder, {})
        if list_time_left() > 0 or not st:
            try:
                progressed |= update_catalog(src, cat, st)
                listed += 1
            except FetchError as e:
                print(f"::warning::[{src.folder}] 목록 실패: {e}")
            save_catalog(src, cat)
        else:
            print(f"[목록] {src.folder}: 목록 몫의 시간이 다 돼 이번에는 건너뜀")
        cats[src.folder] = cat
        have = stored(src)
        fails = state["fails"].setdefault(src.folder, {})
        pending = [it for id_, it in cat.items() if id_ not in have and fails.get(id_, 0) < MAX_FAILS]
        pending.sort(key=src.date, reverse=True)
        if LIMIT:
            pending = pending[:LIMIT]
        queues.append([(src, it) for it in pending])
        print(f"[대기] {src.folder}: 목록 {len(cat)}건, 저장 {len(have)}건, 이번에 받을 것 {len(pending)}건")

    if sources and not listed and not any(cats.values()):
        sys.exit("[오류] 모든 목록을 못 받음 (인증키·IP 등록·서버 상태 확인). refs/ 는 바꾸지 않음")

    tasks = interleave(queues)
    done = {s.folder: 0 for s in sources}
    lock = threading.Lock()

    def work(src, item):
        if over_budget():
            raise OutOfTime()
        return fetch_one(src, item)

    fetched = 0
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(work, src, it): (src, it) for src, it in tasks}
        for f in as_completed(futures):
            src, it = futures[f]
            id_ = it[src.id_field]
            try:
                f.result()
            except OutOfTime:
                continue
            except Exception as e:   # 한 건 실패는 기록만 하고 계속
                with lock:
                    n = state["fails"][src.folder].get(id_, 0) + 1
                    state["fails"][src.folder][id_] = n
                if n >= MAX_FAILS:
                    print(f"::warning::[{src.folder}] {id_} {MAX_FAILS}회 실패로 건너뜀: {e}")
                continue
            with lock:
                state["fails"][src.folder].pop(id_, None)
                done[src.folder] += 1
                fetched += 1
                if fetched % 500 == 0:
                    mins = (time.monotonic() - STARTED) / 60
                    print(f"[진행] {fetched}건 받음 ({mins:.0f}분)")

    summary, remaining = [], 0
    for src in SOURCES:
        cat = cats.get(src.folder)
        if cat is None:
            cat = load_catalog(src)
        st = state["scan"].get(src.folder, {})
        have = len(stored(src))
        fails = state["fails"].get(src.folder, {})
        failed = sum(1 for v in fails.values() if v >= MAX_FAILS)
        left = max(0, len(cat) - have - failed)
        scan_txt = "완료" if st.get("complete") else (f"{st.get('page', 1) - 1}/{st['pages']}쪽" if st.get("pages") else "시작 전")
        if src in sources:
            # 목록을 덜 훑었으면 남은 게 있다고 친다(행정규칙은 매번 새로 훑으므로 빼고)
            remaining += left + (0 if st.get("complete") or src.current else 1)
        summary.append(dict(folder=src.folder, stored=have, target=len(cat), left=left, failed=failed, scan=scan_txt))
        if src in sources:
            print(f"[완료] {src.folder}: 이번 {done.get(src.folder, 0)}건, 저장 {have}/{len(cat)}, 남음 {left}, 목록 {scan_txt}")
    save_state(state)
    write_readme(summary)

    progress = 1 if fetched or progressed else 0
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"fetched={fetched}\nremaining={remaining}\nprogress={progress}\n")
    print(f"[요약] 이번에 {fetched}건 받음, 남은 것 {remaining}건, "
          f"{(time.monotonic() - STARTED) / 60:.0f}분 걸림")


if __name__ == "__main__":
    main()
