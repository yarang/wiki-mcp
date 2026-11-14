# wiki-mcp CLI JSON 계약 (v0.3.0)

> 대상: wiki-mcp 0.3.0의 `kb` 하위 명령을 호출하는 데스크톱 앱(Workflow Manager 등).
> 확인 날짜: 2026-11-15. 이 문서가 계약이다 — 변경은 시맨틱 버전과 함께 알린다.

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
