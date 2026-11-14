"""KB 저장소 — 폴더 구조, sources.yaml, manifest.json, 추출 파일.

모든 쓰기는 KB 폴더 안에서만 일어난다(원본 보존 약속). YAML은 새 의존성을
만들지 않고 python-frontmatter가 끌어오는 PyYAML을 쓴다. 쓰기는 임시 파일 +
flush/fsync + os.replace 로 원자적이게 한다(읽는 도중 반쪽 파일을 보지
않게). Windows 에서 교체 순간의 공유 위반은 잠깐 기다렸다 재시도한다(H7).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from wiki_mcp.kb.errors import KBError

MANIFEST_VERSION = 1

# 쓰기 직렬화 잠금(H7) — add_source·remove_source·스캔 저장이 같은 잠금을 잡는다.
# 스캔의 장기 잠금은 scan.lock, 이쪽은 sources/manifest 교체 직전의 짧은 잠금.
_REPLACE_RETRIES = 10
_REPLACE_WAIT = 0.05
_READ_RETRIES = 5
_READ_WAIT = 0.03
STALE_KB_LOCK_SECONDS = 600.0


class _SourceEntry(BaseModel):
    """sources.yaml 항목 모델(M8) — 형식 오류는 corrupt_sources 로.

    extra="allow": 저장된 다른 필드를 보존해 round-trip 이 깨지지 않게.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    path: str
    include: list[str] = []
    exclude: list[str] = []
    added_at: str = ""


def now_iso() -> str:
    """UTC ISO-8601 타임스탬프 — manifest·last_scan 기록용."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_rel_path(rel: str | Path) -> str:
    """상대 경로를 manifest 키 규칙으로 정규화: '/' 구분 + NFC.

    macOS 는 파일명을 NFD 로 저장하므로 같은 파일이 두 키가 되지 않게 NFC 로
    정규화한다. Windows 는 대소문자만 다른 경로가 같은 파일이므로 비교 시
    rel_key() 를 쓴다.
    """
    parts = Path(str(rel)).parts
    normalized = unicodedata.normalize("NFC", "/".join(parts))
    return normalized.lstrip("/")


def rel_key(rel: str) -> str:
    """rel_path 비교 키 — Windows 에서는 대소문자를 무시한다."""
    return os.path.normcase(rel) if os.name == "nt" else rel


def path_key(p: Path) -> str:
    """절대 경로 비교 키 — Windows 에서는 대소문자·구분자를 무시한다."""
    return os.path.normcase(str(p))


def is_same_or_inside(child: Path, ancestor: Path) -> bool:
    """child 가 ancestor 와 같거나 그 안에 있는가 (Windows 대소문자 무시)."""
    c, a = path_key(child), path_key(ancestor)
    if c == a:
        return True
    return c.startswith(a + os.sep)


def reject_remote_source_path(raw: object) -> None:
    """UNC·장치 네임스페이스 경로를 문자열 단계에서 거부한다(S4).

    `\\\\server\\share`·`//server/share`·`\\\\?\\`·`\\\\.\\` 경로는 exists() 한 번으로
    SMB 왕복(NTLM 자격증명 유출)이 일어날 수 있어, 파일시스템 함수를 부르기
    전에 끊는다. add_source 에서는 expanduser/resolve 전후, load_sources 에서도
    같은 검사를 쓴다.
    """
    s = str(raw)
    norm = s.replace("/", "\\")
    if norm.startswith("\\\\") or s.startswith("//"):
        raise KBError(
            "source_remote_path",
            f"네트워크(UNC)·장치 경로는 등록할 수 없다: {s!r}",
        )


def atomic_write_text(path: Path, data: str) -> None:
    """임시 파일 쓰고 원자적 교체(H7) — flush+fsync 후 같은 디렉토리에서 rename.

    Windows 는 열려 있는 파일의 replace 가 PermissionError(공유 위반)를 낸다 —
    바이러스 검사·인덱서가 잠깐 잡은 경우 50ms 간격으로 최대 10회 재시도한다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp_name, path)
                break
            except PermissionError:
                if attempt == _REPLACE_RETRIES - 1:
                    raise
                time.sleep(_REPLACE_WAIT)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_text_retry(path: Path) -> str:
    """읽기도 짧게 재시도한다(H7) — 교체 순간과 읽기가 겹치는 Windows 대비."""
    for attempt in range(_READ_RETRIES):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == _READ_RETRIES - 1:
                raise
            time.sleep(_READ_WAIT)
    raise PermissionError(str(path))  # unreachable — 정적 가독용


@contextlib.contextmanager
def kb_lock(kb: "KnowledgeBase", *, timeout: float = 10.0, now=time.time):
    """쓰기 직렬화 잠금 `.kb/kb.lock`(H7) — add/remove/스캔 저장이 공유한다.

    O_EXCL 로 잡고 최대 timeout 초 기다린다(50ms 간격). 이미 오래된 잠금은
    이기고 들어간다. 종료 시 내 잠금만 지운다.
    """
    kb.meta_dir.mkdir(parents=True, exist_ok=True)
    lock_path = kb.meta_dir / "kb.lock"
    fd: int | None = None
    deadline = now() + timeout
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except FileExistsError:
            try:
                age = now() - lock_path.stat().st_mtime
            except OSError:
                age = 1e9
            if age > STALE_KB_LOCK_SECONDS:
                with contextlib.suppress(OSError):
                    lock_path.unlink()  # 오래된 잠금 — 치우고 다시
                continue
            if now() >= deadline:
                raise KBError("kb_busy", f"KB 쓰기 잠금을 얻지 못했다: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            owner = int(lock_path.read_text(encoding="ascii").strip() or -1)
        except (OSError, ValueError):
            owner = -1
        if owner == os.getpid():
            with contextlib.suppress(OSError):
                lock_path.unlink()


class KnowledgeBase:
    """KB 루트 폴더의 읽기·쓰기 진입점.

    폴더 구조:
        <KB>/sources.yaml          원본 폴더 목록
        <KB>/.kb/manifest.json     파일별 상태
        <KB>/.kb/extract/*.md      추출 결과 (sha256 앞 32자 이름, 내용 공유)
        <KB>/.kb/last_scan.json    마지막 스캔 결과와 변경 목록
        <KB>/.kb/scan.lock         스캔 동시 실행 방지
        <KB>/wiki/                 노트 (기존 WIKI_PATH 역할)
    """

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.sources_file = self.root / "sources.yaml"
        self.meta_dir = self.root / ".kb"
        self.manifest_file = self.meta_dir / "manifest.json"
        self.extract_dir = self.meta_dir / "extract"
        self.last_scan_file = self.meta_dir / "last_scan.json"
        self.lock_file = self.meta_dir / "scan.lock"
        self.wiki_dir = self.root / "wiki"

    # ─── 초기화 ──────────────────────────────────────────────
    def init(self) -> dict:
        """빈 KB 구조를 만든다. 이미 있으면 그대로 둔다(멱등)."""
        created: list[str] = []
        for d in (self.root, self.meta_dir, self.extract_dir, self.wiki_dir):
            if not d.exists():
                d.mkdir(parents=True, exist_ok=True)
                created.append(str(d))
        if not self.sources_file.exists():
            self.save_sources([])
            created.append(str(self.sources_file))
        return {"kb_dir": str(self.root), "created": created}

    def require_initialized(self) -> None:
        if not self.sources_file.exists():
            raise KBError(
                "kb_not_initialized",
                f"KB가 초기화되지 않았다: {self.root} — `wiki-mcp kb init --kb` 먼저",
            )

    # ─── sources.yaml ────────────────────────────────────────
    def load_sources(self) -> list[dict]:
        if not self.sources_file.exists():
            return []
        raw = _read_text_retry(self.sources_file)
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as e:
            raise KBError("corrupt_sources", f"sources.yaml 을 파싱하지 못했다: {e}") from e
        if isinstance(data, list) or not isinstance(data, dict):
            raise KBError("corrupt_sources", "sources.yaml 최상위는 매핑(sources:)이어야 한다")
        sources = data.get("sources")
        if sources is None:
            return []
        if not isinstance(sources, list):
            raise KBError("corrupt_sources", "sources 항목은 리스트여야 한다")
        validated: list[dict] = []
        for s in sources:  # 항목 단위 검증(M8) — pydantic
            try:
                entry = _SourceEntry.model_validate(s)
            except ValidationError as e:
                raise KBError("corrupt_sources", f"sources.yaml 항목 형식이 잘못됐다: {e}") from e
            validated.append(entry.model_dump())
        ids = [s["id"] for s in validated]
        if len(ids) != len(set(ids)):  # id 중복은 즉시 오류(H6)
            raise KBError("corrupt_sources", "sources.yaml에 중복된 id가 있다")
        for s in validated:  # 저장소 오염 방어 — UNC 경로는 읽는 시점에도 거부(S4)
            reject_remote_source_path(s.get("path"))
        return validated

    def save_sources(self, sources: list[dict]) -> None:
        payload = yaml.safe_dump(
            {"sources": sources},
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        atomic_write_text(self.sources_file, payload)

    # ─── manifest.json ───────────────────────────────────────
    def load_manifest(self) -> dict:
        if not self.manifest_file.exists():
            return {"version": MANIFEST_VERSION, "entries": []}
        try:
            data = json.loads(_read_text_retry(self.manifest_file))
        except (json.JSONDecodeError, OSError) as e:
            raise KBError("corrupt_manifest", f"manifest.json을 읽지 못했다: {e}")
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise KBError("corrupt_manifest", "manifest.json 형식이 잘못됐다")
        return data

    def save_manifest(self, manifest: dict) -> None:
        manifest.setdefault("version", MANIFEST_VERSION)
        atomic_write_text(
            self.manifest_file,
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )

    @staticmethod
    def entry_index(manifest: dict) -> dict[tuple[str, str], dict]:
        """(source_id, rel_key) → entry. rel_path 는 NFC 로 저장되어 있다."""
        return {(e["source_id"], rel_key(e["rel_path"])): e for e in manifest["entries"]}

    # ─── 추출 파일 ───────────────────────────────────────────
    @staticmethod
    def extract_id(sha256: str) -> str:
        return sha256[:32]

    def extract_path(self, sha256: str) -> Path:
        return self.extract_dir / f"{self.extract_id(sha256)}.md"

    # ─── last_scan.json ──────────────────────────────────────
    def load_last_scan(self) -> dict | None:
        if not self.last_scan_file.exists():
            return None
        try:
            return json.loads(_read_text_retry(self.last_scan_file))
        except (json.JSONDecodeError, OSError):
            return None

    def save_last_scan(self, payload: dict) -> None:
        atomic_write_text(
            self.last_scan_file,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
