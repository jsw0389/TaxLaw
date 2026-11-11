#!/usr/bin/env python3
"""법제처 Open API(국가법령정보 공동활용)로 세법 본문을 받아 laws/ 에 텍스트로 저장한다.

- 환경변수 LAW_OC: open.law.go.kr 인증키(OC)
- 대상 법령은 아래 LAWS 목록. 추가하려면 정확한 법령명을 한 줄 넣으면 된다.
- 결과: laws/<법령명>.txt (조문·항·호·목을 들여쓰기한 평문), laws/README.md (목록·시행일)
- 실행일 같은 매번 바뀌는 값은 파일에 쓰지 않는다. 조문이 바뀐 주에만 커밋이 생긴다.
- 한 법령이 실패해도(이름 불일치·접속 오류) 나머지는 계속 받는다. 실패한 법령은 기존 파일을 그대로 두고,
  README.md 에 지난번 행을 유지한다. 실패 목록은 Actions 로그에 경고(::warning::)로 남는다.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

LAWS = [
    "국세기본법",
    "국세기본법 시행령",
    "소득세법",
    "소득세법 시행령",
    "법인세법",
    "법인세법 시행령",
    "부가가치세법",
    "부가가치세법 시행령",
    "상속세 및 증여세법",
    "상속세 및 증여세법 시행령",
    "조세특례제한법",
    "조세특례제한법 시행령",
    "지방세법",
    "지방세법 시행령",
    # 2026-11-02 추가: 시행규칙·기타 세법 (조문 미확인 줄이기)
    "국세기본법 시행규칙",
    "소득세법 시행규칙",
    "법인세법 시행규칙",
    "부가가치세법 시행규칙",
    "상속세 및 증여세법 시행규칙",
    "조세특례제한법 시행규칙",
    "개별소비세법",
    "개별소비세법 시행령",
    "종합부동산세법",
    "종합부동산세법 시행령",
    "종합부동산세법 시행규칙",
    "지방세특례제한법",
    "지방세특례제한법 시행령",
    "자동차관리법 시행규칙",
    # 2026-11-11 추가: 나머지 국세·지방세 법령. 이름이 틀리면 그 법령만 경고 남기고 건너뛴다.
    # 판례·해석례·행정규칙은 scripts/fetch_refs.py 가 refs/ 에 따로 받는다.
    "국세징수법",
    "국세징수법 시행령",
    "국세징수법 시행규칙",
    "국제조세조정에 관한 법률",
    "국제조세조정에 관한 법률 시행령",
    "국제조세조정에 관한 법률 시행규칙",
    "조세범 처벌법",
    "조세범 처벌절차법",
    "조세범 처벌절차법 시행령",
    "세무사법",
    "세무사법 시행령",
    "세무사법 시행규칙",
    "개별소비세법 시행규칙",
    "교통ㆍ에너지ㆍ환경세법",
    "교통ㆍ에너지ㆍ환경세법 시행령",
    "교통ㆍ에너지ㆍ환경세법 시행규칙",
    "주세법",
    "주세법 시행령",
    "주세법 시행규칙",
    "인지세법",
    "인지세법 시행령",
    "인지세법 시행규칙",
    "증권거래세법",
    "증권거래세법 시행령",
    "증권거래세법 시행규칙",
    "교육세법",
    "교육세법 시행령",
    "교육세법 시행규칙",
    "농어촌특별세법",
    "농어촌특별세법 시행령",
    "농어촌특별세법 시행규칙",
    "지방세기본법",
    "지방세기본법 시행령",
    "지방세기본법 시행규칙",
    "지방세징수법",
    "지방세징수법 시행령",
    "지방세징수법 시행규칙",
    "지방세법 시행규칙",
    "지방세특례제한법 시행규칙",
    "국세와 지방세의 조정 등에 관한 법률",
]

BASE = "https://www.law.go.kr/DRF"
OUT = Path(__file__).resolve().parent.parent / "laws"
OC = os.environ.get("LAW_OC", "").strip()


class FetchError(Exception):
    """법령 하나를 받지 못함. 이 법령만 건너뛰고 나머지는 계속한다."""


def get_json(endpoint, **params):
    """API 호출. 인증키가 로그에 찍히지 않도록 URL은 출력하지 않는다."""
    query = urllib.parse.urlencode({"OC": OC, "type": "JSON", **params})
    req = urllib.request.Request(
        f"{BASE}/{endpoint}?{query}",
        headers={"User-Agent": "Mozilla/5.0 (jtax-blog law fetch)"},
    )
    label = params.get("query") or params.get("MST")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
            break
        except Exception as e:  # 네트워크 오류는 두 번까지 재시도
            if attempt == 2:
                raise FetchError(f"{endpoint} ({label}) 접속 실패: {e}")
            time.sleep(10 * (attempt + 1))  # 10초, 20초 (러너 DNS 일시 오류 대비)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # 인증 실패·IP 미등록이면 보통 여기로 온다(HTML이나 오류 문구가 돌아옴)
        raise FetchError(f"{endpoint} ({label}) 응답이 JSON이 아님. 앞부분:\n{body[:800]}")


def as_list(x):
    """API는 항목이 1개면 dict, 여러 개면 list로 준다. 항상 list로 맞춘다."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def text(x):
    """내용 필드는 문자열이거나 문자열 목록(중첩 가능)이다. 한 덩어리 문자열로 편다."""
    if x is None:
        return ""
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, list):
        return "\n".join(t for t in (text(i) for i in x) if t)
    return ""


def indent(s, n):
    pad = " " * n
    return "\n".join(pad + line.strip() for line in s.splitlines() if line.strip())


def find_mst(name):
    data = get_json("lawSearch.do", target="law", query=name, display=100)
    root = data.get("LawSearch")
    if not isinstance(root, dict):
        raise FetchError(f"검색 응답 형식이 예상과 다름 ({name}):\n{json.dumps(data, ensure_ascii=False)[:800]}")
    found = [l for l in as_list(root.get("law")) if str(l.get("법령명한글", "")).strip() == name]
    if not found:
        names = [l.get("법령명한글") for l in as_list(root.get("law"))]
        raise FetchError(f"'{name}' 정확히 일치하는 법령 없음. 검색 결과: {names}")
    current = [l for l in found if l.get("현행연혁코드") == "현행"] or found
    return str(current[0]["법령일련번호"])


def render(name, mst):
    data = get_json("lawService.do", target="law", MST=mst)
    root = data.get("법령")
    if not isinstance(root, dict):
        raise FetchError(f"본문 응답 형식이 예상과 다름 ({name}):\n{json.dumps(data, ensure_ascii=False)[:800]}")
    info = root.get("기본정보", {}) or {}
    meta = {
        "시행일자": str(info.get("시행일자", "")),
        "공포일자": str(info.get("공포일자", "")),
        "공포번호": str(info.get("공포번호", "")),
    }
    lines = [
        f"{name} | 시행 {meta['시행일자']} | 공포 {meta['공포일자']} 제{meta['공포번호']}호 | MST {mst}",
        "출처: 국가법령정보센터 Open API (scripts/fetch_laws.py 로 자동 수집)",
    ]
    units = as_list((root.get("조문") or {}).get("조문단위"))
    if not units:
        raise FetchError(f"'{name}' 조문이 비어 있음")
    for u in units:
        head = text(u.get("조문내용"))
        lines.append("")
        if head:
            lines.append(head)
        if u.get("조문여부") == "전문":  # 편·장·절 제목
            continue
        for hang in as_list(u.get("항")):
            t = text(hang.get("항내용"))
            if t:
                lines.append(indent(t, 2))
            for ho in as_list(hang.get("호")):
                t = text(ho.get("호내용"))
                if t:
                    lines.append(indent(t, 4))
                for mok in as_list(ho.get("목")):
                    t = text(mok.get("목내용"))
                    if t:
                        lines.append(indent(t, 6))
    return "\n".join(lines) + "\n", meta, len(units)


def old_rows():
    """지난번 README.md 의 표 행을 법령명별로 읽는다. 이번에 실패한 법령의 행을 유지하는 데 쓴다."""
    rows = {}
    f = OUT / "README.md"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 4 and cells[1].endswith(".txt"):
                rows[cells[0]] = line
    return rows


def main():
    if not OC:
        sys.exit("[오류] 환경변수 LAW_OC 가 비어 있음 (GitHub Secrets 에 LAW_OC 등록 필요)")
    OUT.mkdir(exist_ok=True)
    prev = old_rows()
    done = {}
    errors = {}

    def fetch(name):
        fname = name.replace(" ", "_") + ".txt"
        try:
            mst = find_mst(name)
            time.sleep(1)
            body, meta, n = render(name, mst)
        except FetchError as e:
            errors[name] = e
            return
        errors.pop(name, None)
        (OUT / fname).write_text(body, encoding="utf-8")
        done[name] = f"| {name} | {fname} | {meta['시행일자']} | {meta['공포일자']} 제{meta['공포번호']}호 |"
        print(f"[완료] {name}: 시행 {meta['시행일자']}, 조문단위 {n}개 → laws/{fname}")

    for name in LAWS:
        fetch(name)
        time.sleep(1)

    # 접속 오류는 일시적인 경우가 많다. 실패한 것만 잠시 뒤 한 번 더 시도한다.
    if errors:
        print(f"[재시도] {len(errors)}건, 60초 후 다시 시도: {', '.join(errors)}")
        time.sleep(60)
        for name in list(errors):
            fetch(name)
            time.sleep(1)

    failed = [n for n in LAWS if n in errors]
    for name in failed:
        print(f"::warning::[실패] {name}: {errors[name]}")
    rows = [done[n] if n in done else prev[n] for n in LAWS if n in done or n in prev]

    if len(failed) == len(LAWS):
        sys.exit("[오류] 모든 법령 수집 실패 (인증키·IP 등록 확인). laws/ 는 바꾸지 않음")

    md = [
        "# 세법 원문 (자동 수집)",
        "",
        "국가법령정보센터 Open API에서 GitHub Actions(.github/workflows/fetch-laws.yml)가 주 1회 받아온 현행 법령 본문.",
        "블로그 원고의 조문 대조는 이 폴더를 grep 해서 한다. 국회 심의 중인 개정안·시행 예정 조문은 여기 없다.",
        "시행규칙의 별표·서식(이미지·첨부파일)은 본문에 들어 있지 않다.",
        "판례·조세심판원 재결례·해석례·기본통칙 등은 ../refs/ 에 따로 있다(scripts/fetch_refs.py).",
        "",
        "| 법령 | 파일 | 시행일 | 공포 |",
        "|---|---|---|---|",
    ] + rows
    (OUT / "README.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    if failed:
        print(f"::warning::수집 실패 {len(failed)}건 (기존 파일 유지): {', '.join(failed)}")


if __name__ == "__main__":
    main()
