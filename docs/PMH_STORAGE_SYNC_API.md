# PMH 대응 명세: FF 조회 오류 및 단건 저장소 동기화

작성일: 2026-10-16. 대상: metadata v1 AV DB(JAV_CEN / JAV_UNCEN / WESTERN 및 JAV / WESTERN 인물).

## 실행 책임

PMH가 Plex 기준 대상 수집, 중복 제거, 진행 상태, 취소, 요청 간격, 재시도를 관리한다. FF는 요청한 작품 또는 인물 하나를 동기적으로 처리하고 결과를 반환한다.

FF의 `db_batch_refresh_start`, 배치 상태, 큐, SSE와 연결하지 않는다. 새 API는 Celery 작업을 생성하지 않으며 작업 ID도 발급하지 않는다. FF 설정 화면에서 실행하는 배치는 기존 실행 방식을 유지하고 동일한 단건 처리 함수를 내부에서 재사용한다.

인증은 현재 PMH `ff_request()`의 FF API 키 전달 방식을 그대로 사용한다. 아래 경로는 FF 서버 주소 기준이며, 요청 본문은 JSON이다.

## 1. 일괄 조회 오류 구분

기존 경로를 유지한다.

```text
POST /metadata/api/meta_db/info_batch
POST /metadata/api/meta_db/batch_info
```

```json
{
  "category": "JAV_CEN",
  "codes": ["CDexample1", "CDexample2"],
  "options": {}
}
```

정상 미등록은 HTTP 200이며, 찾은 코드만 `data`에 포함한다. 모두 미등록인 경우:

```json
{
  "ret": "success",
  "category": "JAV_CEN",
  "total_requested": 2,
  "total_found": 0,
  "data": {}
}
```

DB 세션 획득, SQL 조회, 응답 변환 등 내부 실패는 HTTP 500으로 전달한다.

```json
{
  "ret": "error",
  "error_code": "lookup_failed",
  "msg": "메타 DB 조회에 실패했습니다. FF 로그를 확인하세요."
}
```

`/metadata/meta_api`의 `command=info_batch` / `batch_info`도 동일하게 처리한다. 기존 `arg1=category`, `arg2=코드 목록 JSON`, `arg3=options JSON` 형식을 유지한다.

PMH는 기존처럼 최대 100개씩 나누어 조회한다. **HTTP 200 + ret:success를 확인한 응답에서 코드가 빠진 경우만 미등록으로 판단**한다. 실패한 묶음을 빈 결과로 바꾸거나, 실패한 묶음의 작품들을 미등록으로 기록하면 안 된다.

## 2. 작품 단건 미디어 저장소 동기화

```text
POST /metadata/api/meta_db/media_sync
```

```json
{
  "category": "JAV_CEN",
  "code": "CDexample1",
  "options": {
    "skip_existing": true,
    "poster": true,
    "landscape": true,
    "fanart": true,
    "trailer": true
  }
}
```

- `category`: `JAV_CEN`, `JAV_UNCEN`, `WESTERN` 중 하나. 일반 영화/TV는 지원하지 않는다.
- `code`: FF 조회 응답의 정식 `entity.code`. 표시 품번 `ui_code`나 제목을 대신 전달하지 않는다.
- `options`: 생략 가능. 표에 있는 옵션은 모두 기본값이 `true`인 JSON boolean이다. 문자열 `"false"`나 알 수 없는 옵션은 HTTP 400이다.

| 옵션 | 의미 |
|---|---|
| `skip_existing` | 기존 파일 확인 시 재다운로드를 생략하고 필요한 DB 연결만 보완 |
| `poster` | 대표 포스터 |
| `landscape` | 랜드스케이프 커버 |
| `fanart` | 팬아트. FF에 설정된 저장 장수 제한 적용 |
| `trailer` | 공식 트레일러. FF의 해당 카테고리 다운로드 활성화 설정도 적용 |

등록된 원본 미디어 URL을 사용한다. 사이트 메타데이터 재검색·재수집을 수행하지 않는다. 이미지/영상 파일 다운로드 및 로컬/rclone 저장 작업은 수행할 수 있다. 사용자 이미지는 기존 보호 정책을 따른다. `skip_existing:false`는 기존 공식 미디어를 새로 업로드/교체한다.

이미지는 FF의 공통/카테고리별 이미지 저장 설정을 따르고, 트레일러는 FF의 영상 저장 설정을 따른다. 이 API가 저장 모드를 변경하지 않는다. 프리뷰 생성은 포함하지 않으며 기존 `make_preview_clip` API를 별도로 사용한다.

정상 응답 예시:

```json
{
  "ret": "success",
  "code": "CDexample1",
  "category": "JAV_CEN",
  "is_updated": true,
  "skipped": false,
  "changes": ["포스터 GDrive"],
  "failures": [],
  "title_log": "작품 제목",
  "detail_msg": "포스터 GDrive",
  "msg": "포스터 GDrive"
}
```

변경이 없으면 HTTP 200, `is_updated:false`, `skipped:true`, `changes:[]`이다. 원본 URL이 없거나 사용자 이미지 보호로 처리하지 않은 경우도 변경 없음으로 끝날 수 있다.

여러 미디어 중 일부만 실패하면 HTTP 502, `error_code:media_sync_incomplete`이다. **이미 성공한 파일/DB 변경은 유지될 수 있다.** `changes`와 `is_updated`를 보존해서 기록한다.

```json
{
  "ret": "error",
  "error_code": "media_sync_incomplete",
  "code": "CDexample1",
  "category": "JAV_CEN",
  "is_updated": true,
  "skipped": false,
  "changes": ["포스터 GDrive"],
  "failures": ["trailer"],
  "msg": "일부 미디어 처리에 실패했습니다. changes와 failures를 확인하세요."
}
```

`failures`는 `poster`, `landscape`, `fanart:0` 같은 0부터 시작하는 팬아트 인덱스, `trailer`로 구성된다. 트레일러 다운로드 실패 시 기존 FF 정책에 따라 공식 트레일러 URL을 제거하는 DB 변경이 포함될 수 있다. URL 영구 차단 기록은 생성하지 않는다.

## 3. 인물 단건 이미지 저장소 동기화

```text
POST /metadata/api/meta_db/person_image_sync
```

```json
{
  "domain": "JAV",
  "person_idx": "PA10960",
  "options": {
    "skip_existing": true
  }
}
```

서양 인물 예시:

```json
{
  "domain": "WESTERN",
  "person_idx": "PP6af617ce-476b-4111-93c9-6b499bef68b5",
  "options": {"skip_existing": true}
}
```

- `domain`: `JAV` 또는 `WESTERN`. 필수.
- `person_idx`: `PA…`, `PS…`, `PP…`, `PT…` 등 FF의 인물 코드 문자열. 필수. FF 내부 숫자 PK(`MetaPerson.id` / `person_id`)를 보내지 않는다.
- `options.skip_existing`: JSON boolean, 기본값 `true`. 다른 옵션은 지원하지 않는다.

해당 도메인에서 정식 코드와 `alt_indices`를 대소문자 구분 없이 정확히 비교한다. 코드 접두사나 이름으로 추정하지 않는다. 식별자가 여러 인물과 일치하면 임의 선택하지 않고 HTTP 409로 반환한다.

FF 인물 설정이 `gdrive` / `image_server`이면 공식 이미지 캐시를 해당 저장소에 저장하거나 기존 파일과 DB를 연결한다. 사용자 `_user` 이미지, GDS 원본 정보, 다른 저장 방식의 경로/파일 정보는 보존한다. 사용자 이미지를 공식 이미지 다운로드 원본으로 사용하지 않는다. 새 이미지는 임시 파일로 생성한 다음 최종 저장소로 보낸다.

정상 응답 예시:

```json
{
  "ret": "success",
  "domain": "JAV",
  "requested_person_idx": "PA10960",
  "person_idx": "PA10960",
  "storage_type": "gdrive",
  "relative_path": "jav/actors/사/example_PA10960.jpg",
  "google_fileid": "example-file-id",
  "is_updated": true,
  "skipped": false,
  "changes": ["GDrive 이미지 저장", "인물 이미지 DB 연동"],
  "msg": "동기화 완료"
}
```

별칭/병합 전 코드로 요청했다면 `requested_person_idx`는 요청 코드를, `person_idx`는 현재 정식 코드를 반환한다. `relative_path`는 선택된 저장소 루트 아래의 경로다. 로컬 모드의 `google_fileid`는 빈 문자열이다.

다음은 HTTP 200인 정상 생략이다. `is_updated:false`, `skipped:true`, `changes:[]`와 함께 `reason`이 제공된다.

| reason | 의미 |
|---|---|
| `storage_mode_not_enabled` | 인물 설정이 GDS/사이트 직접 표시 등이며 개인 저장소 모드가 아님 |
| `source_unavailable` | 다운로드할 공식 이미지 URL/ID가 없음 |

이미 파일과 DB가 연결된 경우도 `skipped:true`이며, 이 경우 `reason`은 생략될 수 있다.

## 4. 공통 오류 및 PMH 처리

| HTTP | error_code | PMH 처리 |
|---|---|---|
| 400 | `invalid_request`, `unsupported_domain` | 입력/구현 오류. 같은 요청 자동 재시도 제외 |
| 404 | `not_found` | FF DB에 해당 작품/인물 코드가 없음 |
| 409 | `schema_not_writable` | FF 스키마/작업 잠금 상태 확인 후 재시도 |
| 409 | `ambiguous_person` | 인물 식별 충돌. 다른 인물이나 이름으로 대체하지 않음 |
| 500 | `lookup_failed` | 조회 장애. 미등록으로 처리하지 않음 |
| 500 | `database_unavailable`, `storage_sync_failed` | FF 내부/저장소 처리 장애 |
| 502 | `download_failed`, `upload_failed` | 이미지 다운로드/업로드 실패 |
| 502 | `media_sync_incomplete` | 작품 미디어 일부 실패. 성공한 변경 내역도 기록 |

`changes`와 `msg`는 사람에게 보여주는 문구다. PMH의 분기 조건은 HTTP 상태, `ret`, `error_code`, `is_updated`, `skipped`, `reason`, `failures`를 사용한다.

현재 PMH `ff_request()`는 `urllib.error.HTTPError`를 일반 예외로 잡아 응답 본문을 버린다. 조회 장애와 미등록 구분은 현재 로직으로도 되지만, 새 단건 API의 상세 실패/부분 변경을 활용하려면 다음 대응이 필요하다.

1. 일반 통신 예외보다 먼저 `HTTPError`를 처리한다.
2. 오류 본문의 FF JSON을 안전하게 파싱한다.
3. `FfApiError` 등에 HTTP 상태와 응답 payload를 보존해 툴 작업에 전달한다.
4. URL/API 키를 로그에 노출하지 않고, FF 메시지에도 기존 키 마스킹을 유지한다.
5. 오류 응답을 `{}` 또는 정상 생략으로 바꾸지 않는다.

이미지·영상 처리 요청은 오래 걸릴 수 있다. 단건 동기화 timeout은 기존 미디어 처리 요청에 맞춰 충분히 확보한다(초기 권장 300초, 환경에 따라 조정). 클라이언트 타임아웃은 FF 작업의 취소/롤백을 의미하지 않는다. 재시도 전 완료 여부를 조회하거나 `skip_existing:true`로 확인한다.

## 5. 기존 form 명령 사용 시

`POST /metadata/meta_api`로도 같은 처리가 가능하다.

| command | arg1 | arg2 | arg3 |
|---|---|---|---|
| `db_sync_media` | 작품의 정식 code | AV category | options JSON 문자열 |
| `person_sync_image` | 인물의 person_idx | JAV / WESTERN | options JSON 문자열 |

PMH의 기존 `ff_command(config, command, code, category, options)`를 사용할 수 있다. 응답과 HTTP 상태는 JSON API와 동일하다. `db_refresh_image_only`는 재수집 기능으로 유지하며 저장소 동기화 대신 호출하지 않는다.

## 6. PMH 툴 연결 순서

1. 기존 스키마 확인과 `info_batch` 조회를 수행한다. 조회 실패한 묶음은 오류로 기록한다.
2. 작품 동기화는 Plex 대상과 일치한 FF 응답의 `category + code`로 한 작품씩 요청한다.
3. 출연자 동기화는 FF 작품 정보의 인물 코드(`actor_idx`)와 작품 카테고리로 정한 인물 도메인을 사용한다. `JAV_CEN/JAV_UNCEN → JAV`, `WESTERN → WESTERN`이다.
4. 코드가 없는 출연자는 이름으로 대신 요청하지 않고 제외한다. `(domain, person_idx.casefold())`로 중복 제거한다. 반환된 정식 코드와 요청 코드의 대응도 캐시하면 병합 전 코드의 중복 처리를 줄일 수 있다.
5. PMH 자체 작업 상태에 변경/생략/실패를 기록하고 기존 재시도·취소 정책을 적용한다.
6. FF 배치 제어 API와 SSE를 호출하지 않는다. 이미 시작한 단건 HTTP 요청은 PMH 취소 이후에도 FF에서 완료될 수 있다.

FF API/미디어 처리 변경 적용 후에는 FF 및 사용 중인 Celery 워커를 재시작한다. PMH 쪽 구현 변경은 이 명세에 따라 별도로 진행한다.
