"""원본 폴더 관리 — add_source / list_sources / remove_source.

원본 폴더에는 절대 쓰지 않는다. remove_source는 sources.yaml 항목, 해당
manifest 항목, (더 참조하지 않는) 추출 파일만 지운다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.store import KnowledgeBase, is_same_or_inside, now_iso, path_key

# 민감해 보이는 폴더·파일 이름 — 경로의 어느 부분과 겹치면 confirm 을 요구한다.
# deny 목록이 아니라 경고 목록이다: 사용자가 확인하면 등록할 수 있다.
# (부분 문자열 매칭이라, 흔한 낱말("token" 등)은 오탐이 잦아 넣지 않는다.)
SENSITIVE_NAME_PARTS = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
    "id_rsa",
    "password",
    "passwd",
    "credentials",
    "secrets",
    "개인정보",
    "주민",
    "비밀",
    "인사",
    "급여",
    "진료",
    "재무",
)


def _system_folder_candidates(home: Path) -> list[Path]:
    """거부할 시스템 폴더 후보 — 플랫폼 목록 전부를 후보로 두고 존재·포함만 본다."""
    return [
        Path("C:/Windows"),
        Path("C:/Program Files"),
        Path("C:/Program Files (x86)"),
        Path("C:/ProgramData"),
        home / "AppData",
        Path("/System"),
        Path("/Library"),
        home / "Library",
    ]


def _is_forbidden_root(p: Path, home: Path) -> bool:
    """드라이브/파일시스템 루트, 홈 전체, 시스템 폴더는 등록할 수 없다."""
    if p == p.parent:  # 루트 (POSIX "/", Windows "C:\\")
        return True
    if path_key(p) == path_key(home):  # 홈 폴더 전체
        return True
    for cand in _system_folder_candidates(home):
        if is_same_or_inside(p, cand):
            return True
    return False


def _sensitive_matches(path: Path) -> list[str]:
    """경로 어느 부분과도 겹치는 민감 이름."""
    lowered = str(path).lower()
    return [part for part in SENSITIVE_NAME_PARTS if part in lowered]


def _next_source_id(existing: list[dict]) -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"src-{today}-"
    seq = 1 + sum(1 for s in existing if str(s.get("id", "")).startswith(prefix))
    return f"{prefix}{seq:03d}"


def add_source(
    kb: KnowledgeBase,
    path: str | Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    confirm: bool = False,
    *,
    home: Path | None = None,
) -> dict:
    """원본 폴더 등록. 성공 시 {"source": {...}}, 민감 이름이면 등록 없이
    {"warning": {...}}를 돌려준다(경고는 예외가 아니다 — CLI 종료 코드 3).

    경로 검증: 존재하는 폴더, 심볼릭 링크는 해석 후 판정, KB 안/포함 거부,
    기존 원본과 상하위 겹침 거부, 루트·홈·시스템 폴더 거부.
    """
    kb.require_initialized()
    home = home or Path.home()

    raw = Path(str(path)).expanduser()
    if not raw.exists():
        raise KBError("source_not_found", f"경로가 없다: {raw}")
    # 심볼릭 링크는 해석한 실제 경로로 판정한다
    resolved = raw.resolve()
    if not resolved.is_dir():
        raise KBError("source_not_a_directory", f"폴더가 아니다: {resolved}")

    if is_same_or_inside(kb.root, resolved) or is_same_or_inside(resolved, kb.root):
        raise KBError(
            "overlaps_kb",
            f"KB 폴더와 겹치는 경로는 등록할 수 없다: {resolved} (KB: {kb.root})",
        )

    if _is_forbidden_root(resolved, home):
        raise KBError(
            "system_folder",
            f"시스템 폴더·홈 전체·드라이브 루트는 등록할 수 없다: {resolved}",
        )

    sources = kb.load_sources()
    for s in sources:
        sp = Path(str(s.get("path", ""))).expanduser()
        if not str(sp):
            continue
        if is_same_or_inside(resolved, sp) or is_same_or_inside(sp, resolved):
            raise KBError(
                "overlaps_existing",
                f"이미 등록된 원본({s['id']}: {sp})과 겹치는 경로다: {resolved}",
            )

    matched = _sensitive_matches(resolved)
    if matched and not confirm:
        return {
            "warning": {
                "code": "sensitive_name",
                "message": (
                    "민감해 보이는 이름이 경로에 포함돼 등록을 유보했다. "
                    "계속하려면 confirm=True(--confirm)로 다시 호출한다."
                ),
                "matched": matched,
                "path": str(resolved),
            }
        }

    source = {
        "id": _next_source_id(sources),
        "path": str(resolved),
        "include": list(include or []),
        "exclude": list(exclude or []),
        "added_at": now_iso(),
    }
    sources.append(source)
    kb.save_sources(sources)
    return {"source": source}


def list_sources(kb: KnowledgeBase) -> dict:
    kb.require_initialized()
    sources = kb.load_sources()
    entries = kb.entry_index(kb.load_manifest())
    counts: dict[str, int] = {}
    for sid, _rel in entries:
        counts[sid] = counts.get(sid, 0) + 1
    items = []
    for s in sources:
        items.append(
            {
                "id": s["id"],
                "path": s["path"],
                "include": s.get("include", []),
                "exclude": s.get("exclude", []),
                "added_at": s.get("added_at", ""),
                "files": counts.get(s["id"], 0),
            }
        )
    return {"sources": items}


def remove_source(kb: KnowledgeBase, source_id: str, *, remove_extracts: bool = True) -> dict:
    """원본 등록 제거. 원본 폴더는 전혀 건드리지 않는다.

    지우는 것: sources.yaml 항목, manifest 항목, 다른 원본이 참조하지 않는
    추출 파일. 색인은 다음 스캔/색인 구축 시 자연히 반영된다.
    """
    kb.require_initialized()
    sources = kb.load_sources()
    remaining = [s for s in sources if s.get("id") != source_id]
    if len(remaining) == len(sources):
        raise KBError("source_not_found", f"등록된 원본이 없다: {source_id}")

    manifest = kb.load_manifest()
    keep_entries = [e for e in manifest["entries"] if e.get("source_id") != source_id]
    removed_entries = [e for e in manifest["entries"] if e.get("source_id") == source_id]

    removed_extracts: list[str] = []
    if remove_extracts:
        kept_shas = {e.get("sha256") for e in keep_entries if e.get("sha256")}
        for e in removed_entries:
            sha = e.get("sha256")
            if not sha or sha in kept_shas:
                continue  # 다른 파일이 같은 추출을 공유하는 경우 유지
            xp = kb.extract_path(sha)
            if xp.exists():
                removed_extracts.append(xp.name)
                xp.unlink()

    kb.save_sources(remaining)
    kb.save_manifest({"version": manifest.get("version", 1), "entries": keep_entries})
    return {
        "removed": source_id,
        "manifest_entries_removed": len(removed_entries),
        "extracts_removed": removed_extracts,
        "original_untouched": True,
    }
