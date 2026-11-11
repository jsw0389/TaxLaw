# TaxLaw — 세법 원문·판례·해석 모음 (자동 수집)

세무회계 정연이 답변·블로그 원고의 근거를 찾을 때 grep 하는 자료 저장소다.
사람이 손으로 고치지 않는다. GitHub Actions가 국가법령정보센터 Open API 등에서 받아 커밋한다.

```
laws/      세법 본문 (법·시행령·시행규칙 60여 개). scripts/fetch_laws.py, 매주 일요일 03:00
refs/      판례·헌재·조세심판원 재결례·해석례·행정규칙(기본통칙 등) 전문. scripts/fetch_refs.py, 매주 일요일 04:00
           처음 모으는 동안은 남은 게 0이 될 때까지 워크플로가 스스로 이어 받는다
```

목록·시행일·건수는 `laws/README.md`, `refs/README.md`가 기준이다.

## 필요한 폴더만 받기

refs/ 는 커서 통째로 받으면 오래 걸린다. 필요한 폴더만 받는다.

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/jsw0389/TaxLaw taxlaw
cd taxlaw
git sparse-checkout set laws                      # 법령만
git sparse-checkout add refs/판례 refs/해석_국세청   # 필요할 때 더하기
```

## 찾는 법 (토큰 아끼기)

- 법령: 파일을 통째로 읽지 말고 조문만. `grep -n "^제94조" laws/소득세법.txt` → 그 줄부터 몇십 줄만
- 판례·해석: 파일 첫 줄이 `자료 | 번호 | 날짜 | 제목 | ID`. 제목·요지로 먼저 추리고 전문은 필요할 때만
  `grep -rl "양도소득세" refs/조세심판원/2025 | head`

## 필요한 Secret

- `LAW_OC`: open.law.go.kr Open API 인증키(OC). Settings → Secrets and variables → Actions

공개 저장소라 Actions 표준 러너 사용이 무료다. 인증키는 로그·파일에 남기지 않는다.
