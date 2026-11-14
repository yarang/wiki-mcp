"""scan_sources — 결정적 스캔(LLM·네트워크 없음).

원본 폴더를 걸으며 새/바뀜/지움을 판정하고 지원 형식의 텍스트를 KB 의
추출층(.kb/extract/) 으로 옮긴다. 원본에는 한 글자도 쓰지 않는다 — 읽기만
한다. 지식 기반 폴더를 지우면 원본에 흔적이 없다.

상태·사유 코드는 docs/kb-mode.md 의 표가 계약이다:
    ok / failed(unsupported, too_large, encrypted, corrupt, empty_text,
    locked, extract_error) / deleted. 불안정한 파일(수정 중)은 이번
    스캔에서 건너뛰고 다음 스캔에 다시 본다(unstable, manifest 에 남기지
    않는다). 오류 원문·파일 내용은 manifest 에 넣지 않고 stderr 로그에만
    경로 정도를 남긴다.
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
from wiki_mcp.kb.store import KnowledgeBase, normalize_rel_path, now_iso, rel_key

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
UNSTABLE_SECONDS = 2.0
STALE_LOCK_SECONDS = 600.0

# 파일 이름 기준 제외 — 임시·백업·OS 메타데이터
_SKIP_PREFIXES = (".", "~$", ".~lock")
_SKIP_SUFFIXES = (".tmp", ".bak")
_SKIP_BASENAMES = {"thumbs.db", "desktop.ini"}

_TEXT_EXTS = {".md", ".markdown", ".txt", ".csv"}

# Windows 파일 속성 (stat 모듈에 없는 상수 — Python 3.12 기준 존재하지만 구버전 방어)
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4


def _log(message: str) -> None:
    """진행·실패 로그는 stderr 로만(stdout 은 프로토콜·JSON 계약)."""
    print(message, file=sys.stderr)


def _skip_filename(name: str) -> bool:
    lowered = name.lower()
    if lowered in _SKIP_BASENAMES:
        return True
    return name.startswith(_SKIP_PREFIXES) or name.lower().endswith(_SKIP_SUFFIXES)


def _glob_matches(pattern: str, rel_posix: str, name: str) -> bool:
    """glob 하나 매칭 — 패턴에 '/' 가 있으면 상대 경로 전체, 아니면 파일명."""
    return fnmatch.fnmatch(rel_posix if "/" in pattern else name, pattern)


def _passes_filters(include: list[str], exclude: list[str], rel_posix: str, name: str) -> bool:
    if exclude and any(_glob_matches(g, rel_posix, name) for g in exclude):
        return False
    if include and not any(_glob_matches(g, rel_posix, name) for g in include):
        return False
    return True


def _windows_hidden(st: os.stat_result) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_text_with_fallback(path: Path) -> str:
    """텍스트 파일 읽기 — utf-8 → cp949 순서로 시도한다."""
    raw = path.read_bytes()
    for encoding in ("utf-8", "cp949"):
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
        if len(text.strip()) < 20:
            raise ExtractionError("텍스트가 비었거나 너무 짧다", code="empty_text")
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "\n\n_(원문이 길어 일부만 사용됨)_"
        return "text", text
    text = extract_text(path, max_chars=max_chars, ocr=ocr, ocr_bin=ocr_bin, markers=True)
    return ext.lstrip("."), text


class _ScanLock:
    """`.kb/scan.lock` — 동시 스캔 방지. 10분 넘은 잠금은 죽은 것으로 본다."""

    def __init__(self, kb: KnowledgeBase, now):
        self.path = kb.lock_file
        self.now = now
        self._fd: int | None = None

    def __enter__(self) -> _ScanLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            age = self.now() - self.path.stat().st_mtime
            if age < STALE_LOCK_SECONDS:
                raise KBError(
                    "scan_in_progress",
                    "다른 스캔이 진행 중이다(잠시 후 다시 시도): " + str(self.path),
                )
            _log(f"⚠️ 오래된 스캔 잠금을 무시한다({age:.0f}초): {self.path}")
            try:
                self.path.unlink()  # 죽은 잠금 — 치우고 다시 만든다
            except OSError:
                pass
        try:
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as e:
            raise KBError("scan_in_progress", "다른 스캔이 진행 중이다") from e
        os.write(self._fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass


def _walk_source(root: Path):
    """숨김 디렉토리를 prune 하고 (디렉토리, 파일명) 을 순회한다. symlink 디렉토리는 따라가지 않는다."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        yield Path(dirpath), filenames


def _resolve_symlink(path: Path, root: Path) -> Path | None:
    """심볼릭 링크 파일 — 해석 결과가 원본 안이면 대상 경로, 밖이면 None(건너뜀)."""
    try:
        if not path.is_symlink():
            return path
        target = path.resolve()
        from wiki_mcp.kb.store import is_same_or_inside

        return target if is_same_or_inside(target, root) else None
    except OSError:
        return None


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
) -> dict:
    return {
        "source_id": source_id,
        "rel_path": rel,
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
    from wiki_mcp.kb.store import atomic_write_text

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

    반환: {new, changed, deleted, unchanged, failed, extracted, duration_ms}
    과 변경 목록(changes)은 .kb/last_scan.json 에도 저장된다.
    """
    started = time.perf_counter()
    now = now or time.time
    kb.require_initialized()

    with _ScanLock(kb, now):
        sources = kb.load_sources()
        manifest = kb.load_manifest()
        index = kb.entry_index(manifest)
        seen: set[tuple[str, str]] = set()
        counters = {
            "new": 0,
            "changed": 0,
            "deleted": 0,
            "unchanged": 0,
            "failed": 0,
            "extracted": 0,
        }
        changes: list[dict] = []
        entries: dict[tuple[str, str], dict] = {}

        for src in sources:
            sid = src.get("id", "")
            root = Path(str(src.get("path", ""))).expanduser()
            include = src.get("include") or []
            exclude = src.get("exclude") or []
            if not root.is_dir():
                _log(f"⚠️ 원본 폴더가 없다(건너뜀): {root}")
                continue

            for directory, filenames in _walk_source(root):
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

        # 이번에 못 본 기존 항목 → deleted
        for key, prev in index.items():
            if key in seen:
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

        # 유지된 항목(entries 에 없는 기존 unchanged/failed 항목)을 manifest 에 반영
        # — deleted(seen 밖) 항목은 여기서 되살리지 않는다
        for key, prev in index.items():
            if key in seen and key not in entries:
                prev["scanned_at"] = now_iso()
                entries[key] = prev

        # 삭제된 항목의 추출 파일 — 다른 항목이 참조하지 않으면 제거
        live_shas = {e.get("sha256") for e in entries.values() if e.get("sha256")}
        for key, prev in index.items():
            if key in seen:
                continue
            sha = prev.get("sha256")
            if sha and sha not in live_shas:
                xp = kb.extract_path(sha)
                if xp.exists():
                    xp.unlink()

        kb.save_manifest({"version": manifest.get("version", 1), "entries": list(entries.values())})

        duration_ms = int((time.perf_counter() - started) * 1000)
        results = {**counters, "duration_ms": duration_ms, "changes": changes}
        kb.save_last_scan(
            {
                "scanned_at": now_iso(),
                "results": {k: v for k, v in results.items() if k != "changes"},
                "changes": changes,
            }
        )
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
    try:
        real = _resolve_symlink(fpath, root)
        if real is None:
            return
        st = real.stat()
    except OSError as e:
        _log(f"⚠️ 상태 확인 실패(건너뜀): {fname} ({type(e).__name__})")
        return
    if not stat_module.S_ISREG(st.st_mode):
        return
    if _windows_hidden(st):
        return

    rel = normalize_rel_path(fpath.relative_to(root))
    rel_posix = rel
    if not _passes_filters(include, exclude, rel_posix, fname):
        return

    ext = fpath.suffix.lower()
    key = (sid, rel_key(rel))
    seen.add(key)
    prev = index.get(key)

    def _record(entry: dict, change_status: str | None) -> None:
        entries[key] = entry
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

    if ext not in SUPPORTED_EXTS:
        if ext in KNOWN_UNSUPPORTED_EXTS:
            # .hwp — 바이너리 형식. 사용자 안내를 위해 실패로 기록한다.
            if (
                prev
                and prev.get("size") == st.st_size
                and prev.get("mtime") == st.st_mtime
                and prev.get("status") == "failed"
                and prev.get("reason") == "unsupported"
            ):
                prev["scanned_at"] = now_iso()
                entries[key] = prev
                return
            _record(
                _entry(
                    sid,
                    rel,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    sha256=None,
                    status="failed",
                    reason="unsupported",
                ),
                "failed",
            )
            _log(f"⚠️ 미지원 형식(.hwp): {rel}")
        return  # 그 외 확장자는 조용히 무시한다

    if st.st_size > max_file_size:
        if (
            prev
            and prev.get("size") == st.st_size
            and prev.get("mtime") == st.st_mtime
            and prev.get("status") == "failed"
            and prev.get("reason") == "too_large"
        ):
            prev["scanned_at"] = now_iso()
            entries[key] = prev
            return
        _record(
            _entry(
                sid,
                rel,
                size=st.st_size,
                mtime=st.st_mtime,
                sha256=None,
                status="failed",
                reason="too_large",
            ),
            "failed",
        )
        _log(f"⚠️ 파일이 너무 크다({st.st_size}B): {rel}")
        return

    if now() - st.st_mtime < UNSTABLE_SECONDS:
        # 방금 수정된 파일 — 저장 중일 수 있으니 이번 스캔에서는 건너뛴다
        _log(f"⏳ 수정 직후(다음 스캔에 다시): {rel}")
        seen.discard(key)
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
            _record(
                _entry(
                    sid,
                    rel,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    sha256=None,
                    status="failed",
                    reason="locked",
                ),
                "failed",
            )
            _log(f"🔒 잠긴 파일: {rel}")
            return
        except OSError as e:
            _record(
                _entry(
                    sid,
                    rel,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    sha256=None,
                    status="failed",
                    reason="extract_error",
                ),
                "failed",
            )
            _log(f"⚠️ 해시 실패({type(e).__name__}): {rel}")
            return

    if prev and prev.get("status") == "ok" and prev.get("sha256") == sha256:
        prev["scanned_at"] = now_iso()
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
        _record(
            _entry(
                sid,
                rel,
                size=st.st_size,
                mtime=st.st_mtime,
                sha256=sha256,
                status="failed",
                reason="locked",
            ),
            "failed",
        )
        _log(f"🔒 잠긴 파일: {rel}")
        return
    except ExtractionError as e:
        _record(
            _entry(
                sid,
                rel,
                size=st.st_size,
                mtime=st.st_mtime,
                sha256=sha256,
                status="failed",
                reason=e.code,
            ),
            "failed",
        )
        _log(f"⚠️ 추출 실패({e.code}): {rel}")
        return
    except Exception as e:  # 알 수 없는 추출기 오류 — 코드로만 기록
        _record(
            _entry(
                sid,
                rel,
                size=st.st_size,
                mtime=st.st_mtime,
                sha256=sha256,
                status="failed",
                reason="extract_error",
            ),
            "failed",
        )
        _log(f"⚠️ 추출 오류({type(e).__name__}): {rel}")
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
        ),
        status,
    )
