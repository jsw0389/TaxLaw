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
# 주소를 바꾸는 환경변수는 모의 서버로 시험할 때만 쓴다
BASE = os.environ.get("REFS_BASE", "https://www.law.go.kr/DRF")
TAXLAW = os.environ.get("REFS_TAXLAW", "https://taxlaw.nts.go.kr")
OC = os.environ.get("LAW_OC", "").strip()
BUDGET = float(os.environ.get("REFS_BUDGET_MIN", "200")) * 60
ONLY = {s.strip() for s in os.environ.get("REFS_ONLY", "").split(",") if s.strip()}
LIMIT = int(os.environ.get("REFS_LIMIT", "0") or 0)
WORKERS = 4          # 동시 요청 수. 늘려도 상대 서버 응답이 병목이라 별로 안 빨라진다
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


def law_list_page(target, page):
    """목록 한 쪽(XML). (전체 건수, 항목 목록[dict])"""
    body, _, _ = http(LAW_HOST, law_url("lawSearch.do", target=target, type="XML",
                                        display=PAGE, page=page), label=f"{target} 목록 {page}쪽")
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


def prec_ntst_id(prec_id):
    """국세청 출처 판례: 법제처 판례 화면 → iframe → 넘김을 따라가 ntstDcmId 를 찾는다."""
    page, _, _ = http(LAW_HOST, law_url("lawService.do", target="prec", type="HTML", ID=prec_id),
                      label=f"prec {prec_id} 화면")
    m = NTST_RE.search(page)
    if m:
        return m.group(1)
    m = re.search(r"<iframe\b[^>]*\bsrc\s*=\s*[\"']?\s*([^\"'>\s]+)", page, re.I)
    if not m:
        raise FetchError(f"prec {prec_id} 화면에 본문 틀(iframe)이 없음")
    url = urllib.parse.urljoin("https://www.law.go.kr", html.unescape(m.group(1)))
    for _ in range(4):
        m = NTST_RE.search(url)
        if m:
            return m.group(1)
        host = urllib.parse.urlparse(url).hostname or ""
        if not (host.endswith("law.go.kr") or host.endswith("nts.go.kr") or host in OK_HOSTNAMES):
            raise FetchError(f"prec {prec_id} 넘김이 다른 곳으로 감: {host}")
        is_tax = host.endswith("nts.go.kr") or host == TAX_HOSTNAME
        body, _, loc = http(TAX_HOST if is_tax else LAW_HOST, url,
                            follow=False, label=f"prec {prec_id} 넘김")
        m = NTST_RE.search(body)
        if m:
            return m.group(1)
        if not loc:
            break
        url = urllib.parse.urljoin(url, loc)
    raise FetchError(f"prec {prec_id} 국세법령정보시스템 문서번호를 못 찾음")


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
        return d[:8] if len(d) >= 8 else ""

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
    Source("해석_행안부", "moisCgmExpc", "법령해석일련번호", ("해석일자",), ("안건번호",), ("안건명",),
           keep=keep_mois, need=("안건명",)),
    Source("행정규칙", "admrul", "행정규칙일련번호", ("발령일자", "시행일자"), ("발령번호",), ("행정규칙명",),
           keep=keep_admrul, current=True, need=("소관부처명", "행정규칙명")),
]


# ---------------------------------------------------------------- 목록

def scan(src):
    """목록 전체를 훑어 세법 자료만 고른다. (항목 dict 목록, 목록이 완전한가)"""
    total, items = law_list_page(src.target, 1)
    if not items:
        return [], total == 0
    pages = math.ceil(total / len(items))
    complete = True
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(law_list_page, src.target, p): p for p in range(2, pages + 1)}
        for f in as_completed(futures):
            try:
                items += f.result()[1]
            except FetchError as e:
                complete = False
                print(f"::warning::[{src.folder}] {e}")
    for field in src.need:
        if not any(field in it for it in items):
            print(f"::warning::[{src.folder}] 목록에 '{field}' 칸이 없음 → 고르기가 안 됨. "
                  f"있는 칸: {sorted(items[0])}")
    seen, picked = set(), []
    for it in items:
        id_ = it.get(src.id_field, "")
        if id_ and id_ not in seen and src.keep(it):
            seen.add(id_)
            picked.append(it)
    print(f"[목록] {src.folder}: 전체 {total}건 중 세법 자료 {len(picked)}건"
          + ("" if complete else " (일부 쪽 실패)"))
    return picked, complete


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

def fetch_one(src, item):
    id_ = item[src.id_field]
    title = first(item, src.title_fields)
    origin = "국가법령정보센터 Open API"
    if src.body == "law":
        lines = render_fields(pairs_of(law_detail(src.target, id_)))
    elif src.body == "prec":
        try:
            lines = render_fields(pairs_of(law_detail("prec", id_)))
        except FetchError:
            dcm, editors, url = taxlaw_doc(prec_ntst_id(id_))
            lines = render_taxlaw(dcm, editors)
            origin = f"국세법령정보시스템 {url}"
    else:  # taxlaw
        m = NTST_RE.search(" ".join(v for k, v in item.items() if "링크" in k))
        if m:
            dcm, editors, url = taxlaw_doc(m.group(1))
            lines = render_taxlaw(dcm, editors)
            origin = f"국세법령정보시스템 {url}"
        else:
            lines = render_fields(pairs_of(law_detail(src.target, id_)))
    head = " | ".join(x for x in (src.folder, first(item, src.no_fields), src.date(item), title) if x)
    text = "\n".join([f"{head} | ID {id_}", f"출처: {origin} (scripts/fetch_refs.py 로 자동 수집)", ""] + lines)
    path = src.path(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text.rstrip() + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


# ---------------------------------------------------------------- 실행

def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    state = {k: dict(sorted(v.items())) for k, v in sorted(state.items()) if v}
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def interleave(queues):
    """자료 종류마다 최근 것부터 줄 세운 것을 한 건씩 번갈아 섞는다."""
    out, i = [], 0
    while any(queues):
        for q in queues:
            if i < len(q):
                out.append(q[i])
        i += 1
        if all(i >= len(q) for q in queues):
            break
    return out


def write_readme(summary):
    rows = [f"| {s['folder']} | {s['stored']} | {s['target']} | {s['left']} | {s['failed']} |" for s in summary]
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
        "- 처음 모으는 동안은 '남음'이 0이 될 때까지 워크플로가 스스로 이어 받는다",
        "",
        "| 자료 | 저장 | 대상(목록) | 남음 | 3회 실패로 건너뜀 |",
        "|---|---|---|---|---|",
    ] + rows
    (OUT / "README.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def old_summary():
    rows = {}
    f = OUT / "README.md"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            c = [x.strip() for x in line.strip().strip("|").split("|")]
            if len(c) == 5 and c[1].isdigit():
                rows[c[0]] = dict(folder=c[0], stored=c[1], target=c[2], left=c[3], failed=c[4])
    return rows


def main():
    if not OC:
        sys.exit("[오류] 환경변수 LAW_OC 가 비어 있음 (GitHub Secrets 에 LAW_OC 등록 필요)")
    OUT.mkdir(exist_ok=True)
    state = load_state()
    sources = [s for s in SOURCES if not ONLY or s.folder in ONLY]
    queues, info, scanned, timed_out = [], {}, 0, False

    for src in sources:
        if over_budget():
            print("::warning::목록을 다 훑기 전에 시간이 다 됨")
            timed_out = True
            break
        try:
            items, complete = scan(src)
        except FetchError as e:
            print(f"::warning::[{src.folder}] 목록 실패: {e}")
            continue
        scanned += 1
        have = stored(src)
        fails = state.setdefault(src.folder, {})
        pending = []
        if src.current:
            # 현행본: 이름이 같은데 일련번호가 바뀌었으면(개정) 옛 파일을 지우고 새로 받는다
            want = {it[src.id_field] for it in items}
            for id_, p in have.items():
                if id_ not in want and complete and items:
                    p.unlink()   # 폐지·개정으로 현행이 아니게 된 것. git 이력에는 남는다
            have = {k: v for k, v in have.items() if k in want}
        for it in items:
            id_ = it[src.id_field]
            if id_ not in have and fails.get(id_, 0) < MAX_FAILS:
                pending.append(it)
        pending.sort(key=src.date, reverse=True)
        if LIMIT:
            pending = pending[:LIMIT]
        queues.append([(src, it) for it in pending])
        info[src.folder] = dict(folder=src.folder, target=len(items), have=len(have),
                                skipped=sum(1 for it in items if it[src.id_field] not in have
                                            and fails.get(it[src.id_field], 0) >= MAX_FAILS))
        print(f"[대기] {src.folder}: 저장 {len(have)}건, 이번에 받을 것 {len(pending)}건")

    if sources and not scanned and not timed_out:
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
                    n = state[src.folder].get(id_, 0) + 1
                    state[src.folder][id_] = n
                if n >= MAX_FAILS:
                    print(f"::warning::[{src.folder}] {id_} {MAX_FAILS}회 실패로 건너뜀: {e}")
                continue
            with lock:
                state[src.folder].pop(id_, None)
                done[src.folder] += 1
                fetched += 1
                if fetched % 500 == 0:
                    mins = (time.monotonic() - STARTED) / 60
                    print(f"[진행] {fetched}건 받음 ({mins:.0f}분)")

    prev = old_summary()
    summary, remaining = [], 0
    for src in SOURCES:
        if src.folder in info:
            i = info[src.folder]
            have = len(stored(src))
            fails = state.get(src.folder, {})
            failed = sum(1 for v in fails.values() if v >= MAX_FAILS)
            left = max(0, i["target"] - have - failed)
            remaining += left
            summary.append(dict(folder=src.folder, stored=have, target=i["target"], left=left, failed=failed))
            print(f"[완료] {src.folder}: 이번 {done.get(src.folder, 0)}건, 저장 {have}/{i['target']}, 남음 {left}")
        elif src.folder in prev:
            summary.append(prev[src.folder])
            remaining += int(prev[src.folder]["left"] or 0) if src in sources else 0
    save_state(state)
    write_readme(summary)

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"fetched={fetched}\nremaining={remaining}\n")
    print(f"[요약] 이번에 {fetched}건 받음, 남은 것 {remaining}건, "
          f"{(time.monotonic() - STARTED) / 60:.0f}분 걸림")


if __name__ == "__main__":
    main()
