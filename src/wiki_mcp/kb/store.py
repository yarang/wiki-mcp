"""KB 저장소 — 폴더 구조, sources.yaml, manifest.json, 추출 파일.

모든 쓰기는 KB 폴더 안에서만 일어난다(원본 보존 약속). YAML은 새 의존성을
만들지 않고 python-frontmatter가 끌어오는 PyYAML을 쓴다. 쓰기는 임시 파일 +
os.replace 로 원자적이게 한다(읽는 도중 반쪽 파일을 보지 않게).
"""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml

from wiki_mcp.kb.errors import KBError

MANIFEST_VERSION = 1


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


def atomic_write_text(path: Path, data: str) -> None:
    """임시 파일 쓰고 원자적 교체. 같은 디렉토리에 만들어 rename 이 같은
    파일시스템 안에서 일어나게 한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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
        raw = self.sources_file.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
        sources = data.get("sources") or []
        if not isinstance(sources, list):
            raise KBError("corrupt_sources", "sources.yaml 형식이 잘못됐다")
        return sources

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
            data = json.loads(self.manifest_file.read_text(encoding="utf-8"))
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
            return json.loads(self.last_scan_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def save_last_scan(self, payload: dict) -> None:
        atomic_write_text(
            self.last_scan_file,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
