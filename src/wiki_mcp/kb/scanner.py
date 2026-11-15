"""scan_sources — 결정적 스캔(LLM·네트워크 없음).

원본 폴더를 걸으며 새/바뀜/지움을 판정하고 지원 형식의 텍스트를 KB 의
추출층(.kb/extract/) 으로 옮긴다. 원본에는 한 글자도 쓰지 않는다 — 읽기만
한다. 지식 기반 폴더를 지우면 원본에 흔적이 없다.

상태·사유 코드는 docs/kb-mode.md 의 표가 계약이다:
    ok / failed(unsupported, too_large, encrypted, corrupt, empty_text,
    locked, cloud_only, extract_error) / deleted. 불안정한 파일(수정 중,
    추출 중 변경)은 이번 스캔에서 건너뛰고 **이전 manifest 항목을 이월한다**
    (삭제도 추출 삭제도 아니다). 원본 폴더가 없거나 순회 오류가 나도 그
    원본의 항목은 이월되고 결과의 warnings 에 남는다. 오류 원문·파일 내용은
    manifest 에 넣지 않고 stderr 로그에만 경로 정도를 남긴다.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat as stat_module
import sys
import time
from pathlib import Path

import frontmatter

from wiki_mcp.extract import ExtractionError, extract_text
from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.store import (
    KnowledgeBase,
    atomic_write_text,
    is_same_or_inside,
    kb_lock,
    normalize_rel_path,
    now_iso,
    rel_key,
)

# 지원 확장자 — 이 안의 파일만 추출을 시도한다
SUPPORTED_EXTS = {
    ".md",
    ".markdown",
    ".txt",
    ".csv",
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".html",
    ".htm",
    ".hwpx",
}
# 명시적으로 안내하는 미지원 형식(실패 목록에 reason 과 함께 남는다)
KNOWN_UNSUPPORTED_EXTS = {".hwp"}

MAX_FILE_SIZE = 50 * 1024 * 1024
# 수정 중 판정 — 미래 mtime(시계 오차)도 unstable 로 다룬다
UNSTABLE_SECONDS = 2.0
STALE_LOCK_SECONDS = 600.0
LOCK_HEARTBEAT_SECONDS = 5.0

# 실패 재시도 정책(M3):
#   - locked·extract_error 는 매 스캔 다시 시도한다(일시 실패).
#   - 나머지 영구 실패는 size/mtime 가 바뀔 때만 다시 시도한다.
#   - 같은 실패의 반복은 어느 쪽이든 changes 에 넣지 않는다(상태에는 남긴다).
RETRY_ONLY_ON_CHANGE = {"unsupported", "too_large", "encrypted", "corrupt", "empty_text"}

# 파일 이름 기준 제외 — 임시·백업·OS 메타데이터
_SKIP_PREFIXES = (".", "~$", ".~lock")
_SKIP_SUFFIXES = (".tmp", ".bak")
_SKIP_BASENAMES = {"thumbs.db", "desktop.ini"}

# 민감해 보이는 파일·하위 폴더 이름(M12) — 읽지 않고 조용히 건너뛴다
SENSITIVE_NAME_PARTS = (
    "password",
    "passwd",
    "credential",
    "secret",
    "비밀번호",
    "주민등록",
    ".ssh",
    ".gnupg",
)
SENSITIVE_EXTS = {".kdbx", ".pem", ".key", ".pfx", ".p12", ".env"}

_TEXT_EXTS = {".md", ".markdown", ".txt", ".csv"}

# Windows 파일 속성 (Python 3.10 stat 모듈에 없는 상수 방어)
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# OneDrive 온디맨드(M5) — 읽으면 클라우드에서 내려받게 되는 파일들
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000


def _log(message: str) -> None:
    """진행·실패 로그는 stderr 로만(stdout 은 프로토콜·JSON 계약)."""
    print(message, file=sys.stderr)


def _skip_filename(name: str) -> bool:
    lowered = name.lower()
    if lowered in _SKIP_BASENAMES:
        return True
    return name.startswith(_SKIP_PREFIXES) or name.lower().endswith(_SKIP_SUFFIXES)


def _sensitive_filename(name: str) -> bool:
    """민감해 보이는 파일·폴더 이름 — 읽지 않고 건너뛴다(M12)."""
    lowered = name.lower()
    if Path(name).suffix.lower() in SENSITIVE_EXTS:
        return True
    return any(part in lowered for part in SENSITIVE_NAME_PARTS)


def _glob_matches(pattern: str, rel_posix: str, name: str) -> bool:
    """glob 하나 매칭 — 패턴에 '/' 가 있으면 상대 경로 전체, 아니면 파일명."""
    return fnmatch.fnmatch(rel_posix if "/" in pattern else name, pattern)


def _passes_filters(include: list[str], exclude: list[str], rel_posix: str, name: str) -> bool:
    if exclude and any(_glob_matches(g, rel_posix, name) for g in exclude):
        return False
    if include and not any(_glob_matches(g, rel_posix, name) for g in include):
        return False
    return True


def _file_attributes(st: os.stat_result) -> int:
    return getattr(st, "st_file_attributes", 0)


def _windows_hidden(st: os.stat_result) -> bool:
    return bool(_file_attributes(st) & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM))


def _cloud_only(st: os.stat_result) -> bool:
    """OneDrive 온디맨드 파일 — 읽는 순간 클라우드 다운로드를 유발한다(M5)."""
    return bool(
        _file_attributes(st)
        & (
            _FILE_ATTRIBUTE_OFFLINE
            | _FILE_ATTRIBUTE_RECALL_ON_OPEN
            | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        )
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_text_with_fallback(path: Path) -> str:
    """텍스트 파일 읽기 — utf-8-sig → utf-8 → cp949 순서로 시도한다(L1).

    BOM 이 붙은 Windows 메모장 산출물은 BOM 을 떼고, 옛 한국어 문서는
    cp949 로 읽는다. 어느 쪽도 아니면 corrupt.
    """
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ExtractionError("인코딩을 알 수 없다(utf-8/cp949 모두 실패)", code="corrupt")


def _extract_entry_text(path: Path, *, ocr: bool, ocr_bin, max_chars: int) -> tuple[str, str]:
    """파일에서 (extractor 이름, 마크다운 본문)을 뽑는다. 위치 표지 포함."""
    ext = path.suffix.lower()
    if ext in _TEXT_EXTS:
        text = _read_text_with_fallback(path)
        # empty_text 는 공백을 걷어낸 뒤 0자일 때만(L1) — 서식만 있는 짧은
        # 파일도 내용이 있는 한 버리지 않는다.
        if not text.strip():
            raise ExtractionError("텍스트가 비었다", code="empty_text")
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "\n\n_(원문이 길어 일부만 사용됨)_"
        return "text", text
    text = extract_text(path, max_chars=max_chars, ocr=ocr, ocr_bin=ocr_bin, markers=True)
    return ext.lstrip("."), text


def _pid_alive(pid: int) -> bool:
    """PID 생존 확인(M6) — Windows 는 ctypes OpenProcess, POSIX 는 시그널 0."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 다른 사용자 소유 — 살아는 있다
    except OSError:
        return False


class _ScanLock:
    """`.kb/scan.lock` — 동시 스캔 방지(M6).

    잠금 파일에 PID 를 적는다. 최근 잠금이라도 소유 프로세스가 죽었으면
    무시하고 치운다. 스캔 중에는 주기적으로 mtime 을 갱신해(heartbeat)
    진행 중임을 알리고, 종료 시 내 PID 잠금만 지운다.
    """

    def __init__(self, kb: KnowledgeBase, now):
        self.path = kb.lock_file
        self.now = now
        self._fd: int | None = None
        self._last_beat = 0.0

    def __enter__(self) -> _ScanLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                age = self.now() - self.path.stat().st_mtime
            except OSError:
                age = STALE_LOCK_SECONDS + 1  # stat 경합 — 오래된 것으로 본다
            owner = -1
            try:
                owner = int(self.path.read_text(encoding="ascii").strip() or -1)
            except (OSError, ValueError):
                pass
            if age < STALE_LOCK_SECONDS and _pid_alive(owner):
                raise KBError(
                    "scan_in_progress",
                    "다른 스캔이 진행 중이다(잠시 후 다시 시도): " + str(self.path),
                )
            reason = "오래된" if age >= STALE_LOCK_SECONDS else "소유 프로세스가 없는"
            _log(f"⚠️ {reason} 스캔 잠금을 무시한다({age:.0f}초): {self.path}")
            try:
                self.path.unlink()  # 죽은 잠금 — 치우고 다시 만든다
            except FileNotFoundError:
                pass
            except OSError:
                pass
        try:
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as e:
            raise KBError("scan_in_progress", "다른 스캔이 진행 중이다") from e
        os.write(self._fd, str(os.getpid()).encode("ascii"))
        self.beat(force=True)
        return self

    def beat(self, force: bool = False) -> None:
        """잠금 mtime 갱신 — 스캔이 살아 있음을 알린다(주기적 호출)."""
        if self._fd is None:
            return
        t = self.now()
        if not force and t - self._last_beat < LOCK_HEARTBEAT_SECONDS:
            return
        try:
            os.utime(self.path, None)
        except OSError:
            pass
        self._last_beat = t

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            owner = int(self.path.read_text(encoding="ascii").strip() or -1)
        except (OSError, ValueError):
            owner = -1
        if owner == os.getpid():  # 내 잠금만 지운다
            try:
                self.path.unlink()
            except OSError:
                pass


def _dir_pruned(path: Path, root: Path) -> bool:
    """순회에서 가지치기할 폴더인가 — 숨김·시스템·reparse(정션), 원본 밖 링크(S3)."""
    try:
        st = path.stat()
    except OSError:
        return True  # 상태를 못 보는 폴더는 못 들어간다
    if _windows_hidden(st) or _file_attributes(st) & _FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    try:
        return not is_same_or_inside(path.resolve(), root)
    except OSError:
        return True


def _walk_source(root: Path, carry_prefixes: set[tuple[str, str]]):
    """폴더를 순회해 (디렉토리, 파일명)을 낸다.

    가지치기: '.' 숨김 폴더, 민감 이름 폴더(M12), Windows 숨김·시스템·
    reparse 폴더와 원본 밖을 가리키는 링크(S3). 가지치기한 폴더는 이월
    대상 접두사로 등록해 그 안의 기존 항목이 삭제로 처리되지 않게 한다.
    순회 오류(onerror)도 같은 방식으로 이월한다(H1).
    """

    def _rel_prefix(path: Path) -> str:
        return normalize_rel_path(path.relative_to(root))

    def onerror(err: OSError) -> None:
        target = getattr(err, "filename", None)
        if not target:
            return
        _log(f"⚠️ 폴더 순회 실패(이월): {target} ({type(err).__name__})")
        try:
            carry_prefixes.add(("", _rel_prefix(Path(target))))
        except ValueError:
            pass

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=onerror):
        keep: list[str] = []
        for d in dirnames:
            dp = Path(dirpath) / d
            if d.startswith(".") or _sensitive_filename(d) or _dir_pruned(dp, root):
                if not d.startswith("."):  # 숨김 폴더는 애초에 색인 대상이 아니었다
                    try:
                        carry_prefixes.add(("", _rel_prefix(dp)))
                    except ValueError:
                        pass
                continue
            keep.append(d)
        dirnames[:] = keep
        yield Path(dirpath), filenames


def _entry(
    source_id: str,
    rel: str,
    *,
    size: int,
    mtime: float,
    sha256: str | None,
    status: str,
    reason: str | None = None,
    extract_id: str | None = None,
    title: str = "",
    fs_rel_path: str | None = None,
) -> dict:
    return {
        "source_id": source_id,
        "rel_path": rel,
        "fs_rel_path": fs_rel_path if fs_rel_path is not None else rel,
        "size": size,
        "mtime": mtime,
        "sha256": sha256,
        "status": status,
        "reason": reason,
        "extract_id": extract_id,
        "title": title,
        "scanned_at": now_iso(),
    }


def _write_extract(
    kb: KnowledgeBase, sha256: str, source_id: str, rel: str, extractor: str, title: str, text: str
) -> None:
    """추출 결과를 front matter + 본문으로 쓴다(2-4 형식). 원자적 교체."""
    post = frontmatter.Post(
        content=text,
        source_id=source_id,
        rel_path=rel,
        sha256=sha256,
        extractor=extractor,
        extracted_at=now_iso(),
        title=title,
    )
    atomic_write_text(kb.extract_path(sha256), frontmatter.dumps(post))


def _same_failure(prev: dict | None, entry: dict) -> bool:
    """같은 실패의 반복인가 — size·mtime·사유가 모두 같을 때(M3)."""
    return bool(
        prev
        and prev.get("size") == entry["size"]
        and prev.get("mtime") == entry["mtime"]
        and prev.get("status") == "failed"
        and prev.get("reason") == entry["reason"]
    )


def scan_sources(
    kb: KnowledgeBase,
    *,
    ocr: bool = False,
    ocr_bin=None,
    max_extract_chars: int = 200000,
    max_file_size: int = MAX_FILE_SIZE,
    now=None,
) -> dict:
    """등록된 모든 원본을 스캔한다. 결정적 — 같은 상태면 같은 결과.

    반환: {new, changed, deleted, unchanged, failed, extracted,
    skipped_sensitive, duration_ms, changes, warnings}. 변경 목록(changes)과
    경고(warnings — source_missing 등)는 .kb/last_scan.json 에도 저장된다.
    """
    started = time.perf_counter()
    now = now or time.time
    kb.require_initialized()

    # manifest 손상은 스캔이 복구한다(M9) — 격리 파일로 옮기고 빈 상태로 시작.
    try:
        manifest = kb.load_manifest()
    except KBError as e:
        if e.code != "corrupt_manifest":
            raise
        quarantine = kb.manifest_file.with_name(
            f"manifest.json.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        try:
            os.replace(kb.manifest_file, quarantine)
            _log(f"⚠️ 손상된 manifest 를 격리했다: {quarantine.name} — 빈 상태로 다시 색인한다")
        except OSError:
            pass
        manifest = {"version": 1, "entries": []}

    with _ScanLock(kb, now) as scan_lock:
        sources = kb.load_sources()
        index = kb.entry_index(manifest)
        seen: set[tuple[str, str]] = set()
        counters = {
            "new": 0,
            "changed": 0,
            "deleted": 0,
            "unchanged": 0,
            "failed": 0,
            "extracted": 0,
            "skipped_sensitive": 0,
        }
        changes: list[dict] = []
        warnings: list[dict] = []
        entries: dict[tuple[str, str], dict] = {}
        # (source_id, rel 접두사) — 순회하지 못한 폴더 아래 항목의 이월용
        carry_prefixes: set[tuple[str, str]] = set()
        missing_sources: set[str] = set()

        for src in sources:
            sid = src.get("id", "")
            root = Path(str(src.get("path", ""))).expanduser().resolve()
            include = src.get("include") or []
            exclude = src.get("exclude") or []
            if not root.is_dir():
                # 원본 폴더 자체가 없다 — 삭제가 아니라 이월 + 경고(H1)
                _log(f"⚠️ 원본 폴더가 없다(이월): {root}")
                missing_sources.add(sid)
                warnings.append({"code": "source_missing", "source_id": sid})
                continue

            for directory, filenames in _walk_source(root, carry_prefixes):
                scan_lock.beat()
                for fname in filenames:
                    _process_file(
                        kb,
                        sid,
                        root,
                        directory / fname,
                        fname,
                        include=include,
                        exclude=exclude,
                        index=index,
                        seen=seen,
                        entries=entries,
                        counters=counters,
                        changes=changes,
                        ocr=ocr,
                        ocr_bin=ocr_bin,
                        max_extract_chars=max_extract_chars,
                        max_file_size=max_file_size,
                        now=now,
                    )

        # 이번에 못 본 기존 항목 → deleted. 단, 이월 사유(폴더 없음·순회·민감
        # 스킵 폴더)에 해당하면 항목을 그대로 둔다(H1).
        prefixes_by_sid: dict[str, list[str]] = {}
        for sid_key, prefix in carry_prefixes:
            if sid_key == "":  # _walk_source 가 모르는 원본 — 전 원본에 적용
                for s in sources:
                    prefixes_by_sid.setdefault(s.get("id", ""), []).append(prefix)
            else:
                prefixes_by_sid.setdefault(sid_key, []).append(prefix)

        def _carried_over(prev: dict) -> bool:
            sid_prev = prev.get("source_id", "")
            if sid_prev in missing_sources:
                return True
            rel = prev.get("rel_path", "")
            return any(
                rel == p or rel.startswith(p + "/") for p in prefixes_by_sid.get(sid_prev, ())
            )

        for key, prev in index.items():
            if key in seen:
                continue
            if _carried_over(prev):
                prev["scanned_at"] = now_iso()
                entries[key] = prev
                continue
            counters["deleted"] += 1
            changes.append(
                {
                    "source_id": prev["source_id"],
                    "rel_path": prev["rel_path"],
                    "status": "deleted",
                }
            )
            _log(f"🗑 삭제됨: {prev['source_id']} {prev['rel_path']}")

        # 유지된 항목 반영 — unstable·stat 실패·순회 오류로 못 본 항목은
        # _carry_over 가 entries 에 직접 넣는다(아래 블록은 예비적 안전망).
        for key, prev in index.items():
            if key in seen and key not in entries:
                prev["scanned_at"] = now_iso()
                entries[key] = prev

        # 최종 저장은 쓰기 직렬화 잠금 안에서(H7). 저장 직전에 sources 를 다시
        # 읽어, 스캔 도중 등록 해제된 원본의 항목은 버린다(삭제 원본 보존과 별개).
        with kb_lock(kb):
            live_sids = {s.get("id") for s in kb.load_sources()}
            if live_sids != {s.get("id") for s in sources}:
                entries = {k: v for k, v in entries.items() if k[0] in live_sids}
                changes = [c for c in changes if c.get("source_id") in live_sids]

            # 참조가 끊긴 추출 파일 정리(M4) — changed·deleted·remove 어느 경로든,
            # 남은 항목이 참조하지 않는 추출은 스캔 끝에 모두 지운다(민감 내용 잔존 방지).
            live_extract_ids = {
                KnowledgeBase.extract_id(e["sha256"]) for e in entries.values() if e.get("sha256")
            }
            if kb.extract_dir.exists():
                for xp in kb.extract_dir.glob("*.md"):
                    if xp.stem not in live_extract_ids:
                        try:
                            xp.unlink()
                        except OSError:
                            pass

            kb.save_manifest(
                {"version": manifest.get("version", 1), "entries": list(entries.values())}
            )

            duration_ms = int((time.perf_counter() - started) * 1000)
            results = {
                **counters,
                "duration_ms": duration_ms,
                "changes": changes,
                "warnings": warnings,
            }
            kb.save_last_scan(
                {
                    "scanned_at": now_iso(),
                    "results": {k: v for k, v in results.items() if k != "changes"},
                    "changes": changes,
                }
            )

        # 0.4.0 — 스캔 끝에 연결 원본 표시를 갱신한다(kb_lock 은 이미 빠져나와
        # 재진입이 없고 scan.lock 안이다). links.json 손상 등 KBError 는 물론,
        # 예상 밖 예외도 스캔을 실패시키지 않고 warnings 로 낮춘다 — manifest 는
        # 이미 갱신됐으므로 scan 성공(0) 계약을 지킨다(0.3.0 하위 호환).
        # last_scan.json 에는 orphans 를 넣지 않는다(매 스캔 다시 계산되는 값).
        try:
            from wiki_mcp.kb.orphans import mark_orphans

            results["orphans"] = mark_orphans(kb)
        except KBError as e:
            results["orphans"] = None
            warnings.append({"code": "mark_orphans_failed", "detail": e.code})
        except Exception as e:  # noqa: BLE001 — scan 은 표시 갱신 실패로 죽지 않는다
            _log(f"⚠️ mark_orphans 예상 밖 실패: {type(e).__name__}: {e}")
            results["orphans"] = None
            warnings.append({"code": "mark_orphans_failed", "detail": "internal"})
        return results


def _process_file(
    kb: KnowledgeBase,
    sid: str,
    root: Path,
    fpath: Path,
    fname: str,
    *,
    include: list[str],
    exclude: list[str],
    index: dict,
    seen: set,
    entries: dict,
    counters: dict,
    changes: list,
    ocr: bool,
    ocr_bin,
    max_extract_chars: int,
    max_file_size: int,
    now,
) -> None:
    if _skip_filename(fname):
        return
    if _sensitive_filename(fname):
        counters["skipped_sensitive"] += 1  # 조용히 — 이름은 기록하지 않는다(M12)
        return

    rel = normalize_rel_path(fpath.relative_to(root))
    fs_rel = str(fpath.relative_to(root))  # 파일시스템 표기 그대로(M10 — NFD 등)
    rel_posix = rel
    if not _passes_filters(include, exclude, rel_posix, fname):
        return

    ext = fpath.suffix.lower()
    key = (sid, rel_key(rel))
    prev = index.get(key)

    def _carry_over() -> None:
        """이번 스캔에서 판정을 유보한다 — 기존 항목을 지우지 않고 이월한다(H1).

        ok 상태였던 항목은 unchanged 로 세어 사용자에게 '그대로다'로 보인다.
        """
        seen.add(key)  # deleted 처리 방지
        if prev is not None:
            prev["scanned_at"] = now_iso()
            entries[key] = prev
            if prev.get("status") == "ok":
                counters["unchanged"] += 1

    def _record(entry: dict, change_status: str | None) -> None:
        entries[key] = entry
        seen.add(key)
        if change_status:
            counters[change_status] += 1
            changes.append(
                {
                    "source_id": sid,
                    "rel_path": rel,
                    "status": change_status,
                    **({"reason": entry["reason"]} if entry.get("reason") else {}),
                }
            )

    def _record_failure(entry: dict) -> None:
        """실패 기록 — 같은 실패의 반복이면 changes·카운트에서 제외한다(M3)."""
        if _same_failure(prev, entry):
            entries[key] = entry
            seen.add(key)
            return
        _record(entry, "failed")

    def _skip_retry(entry: dict) -> bool:
        """영구 실패가 그대로면 다시 시도하지 않는다(M3)."""
        return _same_failure(prev, entry) and entry["reason"] in RETRY_ONLY_ON_CHANGE

    try:
        real = fpath.resolve()  # 심볼릭 링크·정션 모두 해석해 판정한다(S3)
        st = real.stat()
    except OSError as e:
        _log(f"⚠️ 상태 확인 실패(이월): {fname} ({type(e).__name__})")
        _carry_over()  # stat 실패는 삭제가 아니다 — 기존 항목을 이월한다(H1)
        return
    if not stat_module.S_ISREG(st.st_mode):
        return
    if not is_same_or_inside(real, root):  # 원본 밖 대상 — 건너뛴다(S3)
        return
    if _windows_hidden(st):
        return

    if ext not in SUPPORTED_EXTS:
        if ext in KNOWN_UNSUPPORTED_EXTS:
            entry = _entry(
                sid,
                rel,
                size=st.st_size,
                mtime=st.st_mtime,
                sha256=None,
                status="failed",
                reason="unsupported",
                fs_rel_path=fs_rel,
            )
            if _skip_retry(entry):
                entries[key] = entry
                seen.add(key)
                return
            _record_failure(entry)
            _log(f"⚠️ 미지원 형식(.hwp): {rel}")
        return  # 그 외 확장자는 조용히 무시한다

    if st.st_size > max_file_size:
        entry = _entry(
            sid,
            rel,
            size=st.st_size,
            mtime=st.st_mtime,
            sha256=None,
            status="failed",
            reason="too_large",
            fs_rel_path=fs_rel,
        )
        if _skip_retry(entry):
            entries[key] = entry
            seen.add(key)
            return
        _record_failure(entry)
        _log(f"⚠️ 파일이 너무 크다({st.st_size}B): {rel}")
        return

    if _cloud_only(st):
        entry = _entry(
            sid,
            rel,
            size=st.st_size,
            mtime=st.st_mtime,
            sha256=None,
            status="failed",
            reason="cloud_only",
            fs_rel_path=fs_rel,
        )
        _record_failure(entry)  # 다운로드되면 속성이 사라지니 매번 다시 본다(M5)
        _log(f"☁️ 클라우드 전용 파일(OneDrive 온디맨드): {rel}")
        return

    if abs(now() - st.st_mtime) < UNSTABLE_SECONDS:
        # 방금 수정됐거나(미래 mtime) 저장 중 — 이번 스캔에서는 이월한다(H1)
        _log(f"⏳ 수정 직후(다음 스캔에 다시): {rel}")
        _carry_over()
        return

    # 크기·mtime 이 같으면 해시를 생략한다(대용량 폴더 재스캔 비용 절감)
    if (
        prev
        and prev.get("size") == st.st_size
        and prev.get("mtime") == st.st_mtime
        and prev.get("sha256")
    ):
        sha256 = prev["sha256"]
    else:
        try:
            sha256 = _sha256_file(real)
        except PermissionError:
            entry = _entry(
                sid,
                rel,
                size=st.st_size,
                mtime=st.st_mtime,
                sha256=None,
                status="failed",
                reason="locked",
                fs_rel_path=fs_rel,
            )
            _record_failure(entry)  # locked — 매 스캔 재시도 그룹
            _log(f"🔒 잠긴 파일: {rel}")
            return
        except OSError as e:
            _log(f"⚠️ 해시 실패({type(e).__name__}, 이월): {rel}")
            _carry_over()
            return

    if prev and prev.get("status") == "ok" and prev.get("sha256") == sha256:
        # unchanged — size/mtime 은 지금 값으로 갱신해 다음 해시 생략을 살린다(M4)
        seen.add(key)
        prev["scanned_at"] = now_iso()
        prev["size"] = st.st_size
        prev["mtime"] = st.st_mtime
        prev["fs_rel_path"] = fs_rel
        entries[key] = prev
        counters["unchanged"] += 1
        return

    # new / changed / 실패 재시도 — 추출한다
    title = fpath.stem or fname
    try:
        extractor, text = _extract_entry_text(
            real, ocr=ocr, ocr_bin=ocr_bin, max_chars=max_extract_chars
        )
    except PermissionError:
        entry = _entry(
            sid,
            rel,
            size=st.st_size,
            mtime=st.st_mtime,
            sha256=sha256,
            status="failed",
            reason="locked",
            fs_rel_path=fs_rel,
        )
        _record_failure(entry)
        _log(f"🔒 잠긴 파일: {rel}")
        return
    except ExtractionError as e:
        entry = _entry(
            sid,
            rel,
            size=st.st_size,
            mtime=st.st_mtime,
            sha256=sha256,
            status="failed",
            reason=e.code,
            fs_rel_path=fs_rel,
        )
        _record_failure(entry)
        _log(f"⚠️ 추출 실패({e.code}): {rel}")
        return
    except Exception as e:  # 알 수 없는 추출기 오류 — 코드로만 기록
        entry = _entry(
            sid,
            rel,
            size=st.st_size,
            mtime=st.st_mtime,
            sha256=sha256,
            status="failed",
            reason="extract_error",
            fs_rel_path=fs_rel,
        )
        _record_failure(entry)
        _log(f"⚠️ 추출 오류({type(e).__name__}): {rel}")
        return

    # TOCTOU(M14) — 추출 사이 파일이 바뀌었으면 이번 결과를 버리고 이월한다.
    try:
        st2 = real.stat()
    except OSError:
        _carry_over()
        return
    if st2.st_size != st.st_size or st2.st_mtime != st.st_mtime:
        _log(f"⏳ 추출 중 파일이 바뀌었다(다음 스캔에 다시): {rel}")
        _carry_over()
        return

    extract_id = kb.extract_id(sha256)
    extract_file = kb.extract_path(sha256)
    if not extract_file.exists():
        _write_extract(kb, sha256, sid, rel, extractor, title, text)
        counters["extracted"] += 1
    # 같은 내용(sha256)의 추출은 하나를 공유한다 — 이미 있으면 다시 쓰지 않는다

    status = "changed" if prev else "new"
    _record(
        _entry(
            sid,
            rel,
            size=st.st_size,
            mtime=st.st_mtime,
            sha256=sha256,
            status="ok",
            extract_id=extract_id,
            title=title,
            fs_rel_path=fs_rel,
        ),
        status,
    )
