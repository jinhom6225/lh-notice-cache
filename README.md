# lh-notice-cache

LH청약알리미 앱이 쓰는 **공급정보·일정 공용 캐시**입니다.

공급정보·상세정보 API는 키 하나당 하루 10,000건까지라서, 앱을 쓰는 모든 기기가 직접 부르면 사용자 수에 비례해 한도에 닿습니다. 이 저장소는 GitHub Actions로 하루 4번(KST 06·12·18·24시 근처) 공고 데이터를 모아 `docs/lh_cache_v1.json` 하나로 만들고, GitHub Pages로 배포합니다. 앱은 이 파일을 먼저 보고, 파일에 없는 공고만 직접 API를 부릅니다.

- 배포 주소: `https://jinhom6225.github.io/lh-notice-cache/lh_cache_v1.json`
- 하루 호출량: 약 200건 안팎 (최악 1,000건 미만, 한 번 실행에 최대 600건). 사용자 수와 무관합니다.

## 데이터 출처·이용 조건

- 출처: **한국토지주택공사** 공공데이터 (data.go.kr)
  - 분양임대공고문 조회 `lhLeaseNoticeInfo1`
  - 공급정보 조회 `getLeaseNoticeSplInfo1` (15056765)
  - 상세정보 조회 `getLeaseNoticeDtlInfo1` (15057999)
- 이용허락범위: **제한 없음** (재제공 가능, 출처 표기 유지). 파일 안 `source`·`license` 항목에도 적혀 있습니다.
- 이 저장소와 파일에는 **API 키도, 개인정보도 없습니다.** 키는 저장소 Secret으로만 Actions에 전달되고, 스크립트는 키를 출력하거나 파일에 쓰지 않습니다(오류 문구에서도 가립니다).

## 파일 구조 (schema 1)

```json
{
  "schema": 1,
  "generated_at": "2026-11-01T06:00:00+09:00",
  "source": "한국토지주택공사 공공데이터(data.go.kr) — 출처: 한국토지주택공사",
  "license": "공공데이터 이용허락범위 제한 없음",
  "stats": { "listed": 0, "notices": 0, "refreshed": 0, "failures": { "list": 0, "supply": 0, "schedule": 0 }, "...": 0 },
  "notices": {
    "<PAN_ID>": {
      "fetched_at": "…",       // 공급·일정을 마지막으로 모두 받은 시각 (null = 아직 못 받음)
      "last_seen_at": "…",     // 목록에서 마지막으로 본 시각
      "status": "공고중",
      "codes": { "UPP_AIS_TP_CD": "06", "CCR_CNNT_SYS_DS_CD": "03", "SPL_INF_TP_CD": "063", "AIS_TP_CD": "10" },
      "supply":   { "dsList01": [ … ], "dsList01Nm": [ … ] },
      "schedule": { "dsSplScdl": [ … ], "dsSplScdlNm": [ … ] }
    }
  }
}
```

- `supply`·`schedule`은 API 응답 블록을 **가공하지 않고** 그대로 담습니다 → 앱의 기존 파서가 그대로 읽습니다.
- `{}` = 정상 응답이었지만 블록이 없음, `null` = 아직 정상 응답을 받지 못함(앱이 직접 API로 보완). 주거복지(13) 공고는 공급정보를 부르지 않아 `supply`가 `null`입니다.

## 갱신 규칙

1. 목록: 상위유형 06·05·39·13 × 상태 공고중·접수중·정정공고중, 최근 90일 게시, 전 페이지(`PG_SZ` 100).
2. 공급·상세를 다시 받는 공고: 새 공고, 지난번에 일부 실패한 공고, **정정공고중으로 바뀐 공고(이후 24시간마다)**, 받은 지 7일 지난 공고. 나머지는 이전 값을 그대로 씁니다(증분).
3. 오류 응답(HTTP 오류, JSON이 아닌 응답·XML `OpenAPI_ServiceResponse`, `SS_CODE`≠`Y`)은 **이전 값을 유지**하고 `stats.failures`에만 셉니다.
4. 목록에서 사라진 공고는 마지막으로 본 뒤 30일이 지나면 지웁니다(그 실행에서 목록 호출이 하나라도 실패했으면 지우지 않음).
5. 목록 호출이 모두 실패하거나 공급·상세 호출이 모두 실패하면 파일을 바꾸지 않고 실행을 실패로 끝냅니다 → 파일이 48시간 넘게 갱신되지 않으면 앱이 낡은 파일로 보고 직접 호출 상한을 올립니다.

`generated_at`이 매번 바뀌므로 정상 실행마다 커밋이 하나씩 생깁니다(하루 4번). 저장소 활동으로 잡혀 예약 실행이 꺼지지 않습니다.

## 처음 설정 (진호님)

1. **Secret 등록:** 저장소 Settings → Secrets and variables → Actions → New repository secret
   - Name: `DATA_GO_KR_API_KEY`
   - Secret: 공급정보·상세정보 API가 승인된 data.go.kr 키 (`f05e…`)
2. **Pages 켜기:** Settings → Pages → Build and deployment → Source: **Deploy from a branch** → Branch: **main**, 폴더 **/docs** → Save
3. **첫 실행:** Actions → "Build LH notice cache" → Run workflow. 끝나면 `docs/lh_cache_v1.json` 커밋이 생기고, 몇 분 뒤 배포 주소에서 열립니다.

Secret을 넣기 전에는 예약 실행이 "DATA_GO_KR_API_KEY is not set" 오류로 바로 실패합니다(정상).

## 로컬 실행·테스트

```bash
python3 -m unittest discover -s tests -v          # 네트워크 없이 단위 테스트
DATA_GO_KR_API_KEY=... python3 scripts/build_cache.py --output docs/lh_cache_v1.json --verbose
```

Python 3.12 이상, 표준 라이브러리만 씁니다.
