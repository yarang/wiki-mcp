# wiki-mcp CLI JSON 계약 (v0.4.0)

> 대상: wiki-mcp 0.4.0의 `kb` 하위 명령을 호출하는 데스크톱 앱(Workflow Manager 등).
> 확인 날짜: 2026-11-15. 이 문서가 계약이다 — 변경은 시맨틱 버전과 함께 알린다.
> 0.3.0 명세는 아래 그대로 남아 있고, 「v0.4.0 추가」 절이 새 명령·오류 코드를
> 정의한다.

## 개요

```
wiki-mcp kb <하위명령> --kb <폴더> [인자...]
```

- 인자 없이 `wiki-mcp`만 실행하면 stdio MCP 서버가 뜬다(이 문서 범위 밖).
- 모든 `kb` 하위 명령은 **stdout에 JSON을 정확히 한 줄**만 출력한다. 로그·진행
  메시지는 stderr로만 간다. stdout을 그대로 JSON 파서에 넘기면 된다.
- stdout 인코딩은 **UTF-8로 고정**한다. Windows cp1252 콘솔에서도 한글 경로·
  메시지가 깨지지 않는다(`PYTHONIOENCODING` 불필요).
- 반환값 형식은 MCP 도구(`add_source` 등)의 JSON 반환값과 같다.

## 종료 코드

| 코드 | 의미 | stdout |
| --- | --- | --- |
| 0 | 성공 | 결과 JSON 1개 |
| 1 | 입력 오류(없는 경로·id, 검증 거부, 인자 오류) | `{"error": {"code", "message"}}` |
| 2 | 내부 오류(예기치 못한 예외) | `{"error": {"code": "internal", "message"}}` |
| 3 | 경고 — 등록을 유보했다(민감 폴더 이름) | `{"warning": {...}}` |

경고(3)는 실패가 아니다. 사용자 확인 후 `--confirm`으로 같은 명령을 다시
부르면 등록된다.

## 하위 명령

### `wiki-mcp kb init --kb DIR`

빈 KB 폴더 구조를 만든다. 이미 있으면 그대로 둔다(멱등).

```json
{"kb_dir": "/home/user/personal-kb", "created": ["/home/user/personal-kb", "..."]}
```

- `created`: 이번 실행에 새로 만든 경로 목록. 재실행하면 빈 배열.

### `wiki-mcp kb add-source --kb DIR PATH [--include GLOB]... [--exclude GLOB]... [--confirm]`

원본 폴더를 등록한다. **원본 폴더에는 절대 쓰지 않는다.**

성공(0):

```json
{"source": {"id": "src-20261115-001", "path": "/abs/path", "include": [], "exclude": [], "added_at": "2026-11-15T09:00:00+00:00"}}
```

경고(3) — 경로에 민감해 보이는 이름(`.ssh`, `급여`, `password`, …)이 있을 때.
등록은 되지 않는다:

```json
{"warning": {"code": "sensitive_name", "message": "...", "matched": ["급여"], "path": "/abs/path"}}
```

거부(1) — `error.code` 목록:

| code | 조건 |
| --- | --- |
| `kb_not_initialized` | `init` 안 한 KB 폴더 |
| `source_not_found` | 존재하지 않는 경로 |
| `source_not_a_directory` | 폴더가 아닌 경로 |
| `source_remote_path` | UNC·장치 경로(`\\server\share`, `\\?\`, `\\.\`, `//…`) — 파일시스템 접근 전에 거부 |
| `overlaps_kb` | KB 폴더 안/겹침 |
| `overlaps_existing` | 이미 등록된 원본의 상위·하위 |
| `system_folder` | 드라이브 루트·홈과 홈의 모든 상위 폴더(`C:\Users`, `/Users`, `/home`)·시스템 폴더(환경 변수 기준: `%SystemRoot%`, `%ProgramFiles%`, `%ProgramData%`, `%LOCALAPPDATA%`, `%APPDATA%`, `/System`, `/Library`, `~/Library`)·공유 마운트(`/Volumes`, `/mnt`, `/media`) |
| `corrupt_sources` | sources.yaml 손상 — 최상위가 리스트, 항목이 문자열, path 없음, include가 문자열, id 중복 |

glob 규칙: 패턴에 `/`가 있으면 원본 루트 기준 상대 경로 전체에, 없으면
파일명에 적용한다(`*.hwpx`, `drafts/*`).

### `wiki-mcp kb remove-source --kb DIR ID`

원본 등록 제거. 원본 폴더는 건드리지 않고 KB 쪽(sources.yaml 항목, manifest
항목, 참조가 끊긴 추출 파일)만 지운다.

```json
{"removed": "src-20261115-001", "manifest_entries_removed": 3, "extracts_removed": ["ab12….md"], "original_untouched": true}
```

### `wiki-mcp kb list-sources --kb DIR`

```json
{"sources": [{"id": "src-20261115-001", "path": "/abs", "include": [], "exclude": [], "added_at": "…", "files": 3}]}
```

`files`는 마지막 스캔 기준 manifest 항목 수.

### `wiki-mcp kb scan --kb DIR`

모든 원본을 스캔한다(결정적, LLM·네트워크 없음). 스캔 중 잠금(`.kb/scan.lock`,
PID 기반)으로 동시 실행을 막는다 — 이미 돌고 있으면 `scan_in_progress`(1).

```json
{"new": 2, "changed": 0, "deleted": 0, "unchanged": 1, "failed": 1, "extracted": 2,
 "skipped_sensitive": 1, "duration_ms": 41,
 "changes": [{"source_id": "src-…", "rel_path": "보고서.hwpx", "status": "new"},
             {"source_id": "src-…", "rel_path": "legacy.hwp", "status": "failed", "reason": "unsupported"}],
 "warnings": []}
```

- `extracted`: 실제로 새로 쓴 추출 파일 수. 같은 내용(sha256)의 파일은 추출
  하나를 공유하므로 `new`보다 작을 수 있다. 재스캔에서 변경이 없으면 0.
- `skipped_sensitive`: 민감 이름(비밀번호·키 파일 등)으로 읽지 않고 건너뛴
  파일 수. 파일 이름은 어디에도 기록되지 않는다.
- `changes`: 이번 스캔의 변경 목록(상태가 바뀐 항목만). 원본은 절대 수정되지
  않는다 — 전후 파일 목록·해시·mtime이 같다는 것이 테스트로 보장된다.
- `warnings`: 원본 폴더가 없는 등 이월 사유(`source_missing`). CLI 는
  changes 를 자르지 않는다(MCP 도구 결과는 최대 50건 + `truncated: true`).
- 수정 중(mtime 2초 미만)·stat 실패·순회 오류 파일은 삭제되지 않고 이전
  상태로 이월된다. 참조가 끊긴 추출 파일은 스캔 끝에 정리된다.
- 실패 사유(`reason`)는 고정 코드: `unsupported` `too_large`(50MB 초과)
  `encrypted` `corrupt`(깨진 파일·zip 폭탄 포함) `empty_text` `locked`
  `cloud_only`(OneDrive 온디맨드) `extract_error`. `locked`·`extract_error`는
  매 스캔 재시도하고 나머지는 size/mtime이 바뀔 때만 재시도한다.
- manifest.json이 손상됐으면 `.kb/manifest.json.corrupt-<타임스탬프>`로
  격리하고 빈 상태로 다시 색인한다(추출 파일은 sha로 재사용).

### `wiki-mcp kb status --kb DIR`

```json
{"sources": [{"id": "src-…", "path": "/abs", "files": 3, "by_status": {"ok": 2, "failed": 1},
              "failed": [{"rel_path": "legacy.hwp", "reason": "unsupported"}]}],
 "last_scan": {"scanned_at": "2026-11-15T09:00:00+00:00",
               "results": {"new": 2, "changed": 0, "deleted": 0, "unchanged": 0, "failed": 1,
                            "extracted": 2, "skipped_sensitive": 1, "duration_ms": 41}}}
```

스캔을 한 번도 돌리지 않았으면 `last_scan`은 `null`. MCP `kb_status` 도구는
여기에 색인 문자 상한 초과 파일 목록(`index_truncated`)을 덧붙인다.

## v0.4.0 추가 — 초안 검토 대기열

0.4.0은 **초안 검토 대기열**을 더한다: LLM(MCP)은 출처가 검증된 초안을
`.kb/drafts/`에 저장만 하고, 사람이 앱에서 승인(`drafts-approve`)해야
`wiki/`에 노트가 생긴다. 승인·반려는 `.kb/history.jsonl`에 append-only로
남는다. 승인된 노트와 원본 파일의 연결은 `.kb/links.json`에 있고, 원본이
지워지거나 바뀌면 스캔 끝에 자동으로 노트 머리말에 표시된다(`mark-orphans`).

### 새 하위 명령

모두 `wiki-mcp kb <명령> --kb DIR …` 형식이고, stdout에 JSON 1줄을 낸다.
새 명령은 경고(종료 3)를 쓰지 않는다.

| 명령 | 인자 | 성공 JSON | 비고 |
| --- | --- | --- | --- |
| `drafts-list` | `[--limit N] [--offset N]` | `{"drafts": [DraftSummary…], "total", "limit", "offset", "max_pending": 200, "corrupt_drafts": [id…]}` | 기본 50, 상한 200. `created_at` 오름차순(FIFO) |
| `drafts-show` | `DRAFT_ID [--context N]` | `{"draft": {…}}` | 기본 600, 상한 2000. **본문·인용·문맥을 가공 없이** 돌려준다 |
| `drafts-approve` | `DRAFT_ID` | `{"approved", "kind", "note_id", "path", "links_updated", "backup", "history_seq"}` | actor=`cli`. 문제가 있으면 승인하지 않고 error |
| `drafts-reject` | `DRAFT_ID [--reason TEXT]` \| `[--all-stale] [--reason TEXT]` | `{"rejected", "history_seq"}` 또는 `{"rejected": [id…], "history_seqs": [seq…]}` | `--reason=-로 시작하는 문장` 한 토큰 형식 지원. `--all-stale`은 `issues`에 `draft_stale`이 있는 초안만 일괄 반려한다(`created_at` 순서). `--all-stale`과 `DRAFT_ID`는 함께 쓸 수 없다(`invalid_arguments`) |
| `drafts-history` | `[--limit N] [--offset N]` | `{"entries": […], "total", "limit", "offset", "corrupt_lines"}` | 기본 50, 상한 500, 최신순 |
| `drafts-save` | `--input FILE` | `{"draft": DraftSummary}` | FILE은 DraftInput JSON(UTF-8, 최대 1MB). actor=`cli`. 스크립트·앱 e2e용 |
| `mark-orphans` | — | mark_orphans 결과(아래) | `scan`이 끝날 때도 자동 실행 |

- `DraftSummary`: `{"id", "kind", "note_id", "title", "domain", "summary",
  "created_at", "created_by", "evidence_count", "sources":
  [{"source_id", "rel_path"}], "issues": […], "approvable": bool}`.
  `issues`는 `draft_stale`·`note_not_found`·`note_conflict`·`note_unreadable`·
  `note_exists`·`note_ambiguous` 순서로 계산하며, `approvable`은
  `issues == []`.
- `drafts-show`의 `draft.evidence[i]`: `source_state`(`current` | `changed` |
  `deleted` | `not_extracted`), `source_path`(원본 절대 경로 또는 `null`),
  `context`(`current`일 때만 `{before, match, after, truncated_before,
  truncated_after}`, 그 밖엔 `null`). update 초안은 `current_note`
  (`{path, content, truncated}` — 최대 100,000자)가 붙는다.
- 초안 id 정규식: `^d-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$`. 근거 인용(quote)은
  추출 텍스트에 NFC·공백 정규화로 실제로 있어야 저장된다 — offset은 **NFC
  정규화한 텍스트 기준**이다(`read_extract`의 offset과 다를 수 있다).
- 상한: 본문 20,000자, 대기 200건, 근거 10개, 인용(정규화 뒤) 8~500자.
- 검증 규칙(0.4.0): 도메인은 전체 100자·세그먼트 5개 이하. note_id·도메인
  세그먼트에 Windows 예약 장치명(`con` `prn` `aux` `nul` `com1-9` `lpt1-9`,
  확장자가 붙어도)을 쓸 수 없다. 태그는 숫자로 시작할 수 없고
  `2026-11-15`처럼 YAML에서 문자열로 다시 읽히지 않는 값도 거부한다.
  한 줄 필드(title·summary·tags·domain·reason)에 제어문자와 양방향
  제어문자(U+200B~200F, U+202A~202E, U+2066~2069, U+FEFF)가 있으면
  거부한다(본문·인용은 bidi 오버라이드만 거부). 승인 직전에 저장된 값을
  같은 검증으로 다시 거른다 — 손으로 고친 초안 파일은
  `invalid_note_id`·`invalid_domain`·`invalid_draft`로 거부된다.

### `scan` 결과에 더해지는 필드

`scan` 성공 JSON에 `"orphans"` 키가 하나 더해진다(0.3.0 필드는 그대로):

```json
{"orphans": {"checked": 12, "marked_missing": ["note-a"], "marked_changed": ["note-b"],
             "cleared": ["note-c"], "unchanged": 8,
             "skipped": [{"note_id": "note-d", "reason": "note_missing"}]}}
```

- `marked_*`·`cleared`는 note_id 오름차순. `unchanged`는 값이 바뀌지 않은
  노트 수(파일을 쓰지 않는다 — 멱등). `skipped.reason`은 `note_missing`·
  `note_ambiguous`·`note_frontmatter_unsupported`·`note_unreadable`
  (인코딩·파일 손상 등으로 노트를 읽지 못해 건너뛴 경우).
- 노트 머리말의 `kb_source_status`가 `"missing"`|`"changed"`로 붙거나
  사라진다. 노트 파일은 지우지 않는다. 표시를 쓸 때 줄끝은 **순수 CRLF
  노트만 CRLF로 보존**되고, LF 노트·LF와 CRLF가 섞인 노트는 LF로
  정규화된다.
- `mark_orphans`가 실패하면(예: links.json 손상 → `corrupt_links`, 예상 밖
  오류 → `internal`) `orphans: null`과 함께 `warnings`에
  `{"code": "mark_orphans_failed", "detail": "<detail>"}`가 남는다 —
  이 경우에도 scan은 성공(0)이다.
- `.kb/last_scan.json`에는 `orphans`를 넣지 않는다(매 스캔 다시 계산하는 값).

### 크기 근거 (앱 `STDOUT_MAX_BYTES` 1MB)

`drafts-show`의 최악 크기: 한글 본문 20,000자(≈60KB), 근거 10개 × (인용
500자 + 문맥 2×2,000자)(≈135KB), update 대상 노트 100,000자(≈300KB). 합계
약 0.5MB로 JSON 이스케이프 여유를 둬도 1MB(1,048,576바이트) 아래다.
인수 테스트(tests/test_cli_drafts.py C5)가 이 상한을 고정한다.

### 새 `error.code`

| code | 명령 | 조건 |
| --- | --- | --- |
| `invalid_draft_id` | show/approve/reject | id가 정규식에 맞지 않음 |
| `draft_not_found` | show/approve/reject | 대기 파일 없음, 이력도 없음 |
| `draft_already_decided` | approve/reject | 이미 승인·반려된 id |
| `corrupt_draft` | show/approve | 초안 파일 손상(reject는 허용) |
| `invalid_draft` | save/reject | 필드 형식·길이·제어문자·예약 표지·태그 규칙·update에 title/tags/domain 지정·quote 길이 |
| `invalid_note_id` | save/approve | `validate_note_id` 거부(승인 때는 저장값 재검증) |
| `invalid_domain` | save/approve | `validate_domain`/`safe_wiki_path` 거부(승인 때는 저장값 재검증) |
| `draft_too_large` | save | 본문 20,000자 초과, `--input` 1MB 초과 |
| `draft_limit_reached` | save | 대기 초안 200건 |
| `draft_duplicate` | save | 같은 note_id의 대기 new 초안 있음 |
| `evidence_source_invalid` | save | 근거 파일이 manifest에 없거나 ok가 아니거나 추출이 없거나 rel_path에 제어문자 |
| `evidence_not_found` | save | 인용이 추출 텍스트에 없음 |
| `note_exists` | save/approve | new 대상 id·경로가 이미 있음 |
| `note_not_found` | save/approve | update 대상 노트 없음 |
| `note_ambiguous` | save/approve | 같은 id 노트가 둘 이상 |
| `note_conflict` | approve | update 대상 노트가 저장 후 바뀜(`kb_source_status` 표시만 바뀐 경우는 제외) |
| `note_unreadable` | approve | update 대상 노트를 읽지 못함(인코딩·파일 손상 등) — 이때도 `drafts-list`·`--all-stale` 목록 계산은 멈추지 않는다 |
| `draft_stale` | approve | 근거 원본의 sha가 바뀌었거나 사라짐 |
| `note_frontmatter_unsupported` | approve(update)/mark-orphans | 머리말 편집 규칙(docs/kb-mode.md) 불충족 |
| `corrupt_links` | approve/mark-orphans | links.json 손상 |

기존 공통 코드(`kb_not_initialized` `kb_busy` `corrupt_manifest` `io_error`
`internal` `invalid_arguments`)도 그대로 나올 수 있다.

### 0.3.0 대비 동작 변경 (KB 모드 MCP)

- KB 모드에서 MCP 직접 쓰기 도구 `create_note`·`update_note`·`add_link`·
  `delete_note`가 **기본 숨겨진다**(승인 관문 우회 방지).
  `WIKI_KB_MCP_DIRECT_WRITE=1`로 다시 노출한다. 비KB 모드는 그대로다.
- MCP에 초안 도구 `save_draft`·`list_drafts`·`get_draft`·`mark_orphans`가
  더해진다. 승인·반려는 `WIKI_KB_MCP_REVIEW=1`일 때만 노출된다.

## 공통 `error.code` (모든 하위 명령)

`kb_not_initialized` `source_not_found` `source_remote_path` `scan_in_progress`
`kb_busy`(쓰기 잠금 대기 초과) `corrupt_sources` `corrupt_manifest`
`invalid_arguments` `unknown_command` `io_error` `internal`

`internal`(2)의 message는 일반 문구다 — 상세(예외 종류·메시지)는 stderr
로그에만 남는다.

## 호출 예 (Rust 앱)

```rust
let out = Command::new("uvx")
    .args(["--from", "git+https://github.com/yarang/wiki-mcp@v0.3.0", "wiki-mcp",
           "kb", "scan", "--kb", kb_dir])
    .output()           // stdout = JSON 1줄 (UTF-8)
    .await?;
let payload: Value = serde_json::from_slice(&out.stdout)?;
match out.status.code() {
    Some(0) => { /* payload = 결과 */ }
    Some(3) => { /* payload.warning — 사용자 확인 요청 */ }
    Some(1 | 2) => { /* payload.error — 표시 */ }
    _ => { /* 프로세스 실패(부팅 오류 등) */ }
}
```
