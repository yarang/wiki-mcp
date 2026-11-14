"""원본 폴더 관리 — add_source / list_sources / remove_source.

원본 폴더에는 절대 쓰지 않는다. remove_source는 sources.yaml 항목, 해당
manifest 항목, (더 참조하지 않는) 추출 파일만 지운다.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.store import (
    KnowledgeBase,
    is_same_or_inside,
    kb_lock,
    now_iso,
    path_key,
    reject_remote_source_path,
)

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
    "인사기록",
    "인사평가",
    "급여",
    "진료",
    "재무",
)

# POSIX 마운트 루트 — 후보 목록이 아니라 "자체와 바로 아래 한 단계"만 거부한다.
# 바로 아래 한 단계(/Volumes/USB 등)가 드라이브 전체를 뜻하기 때문이고,
# /Volumes/USB/자료 처럼 더 깊은 폴더는 특정 폴더 지정이므로 허용한다.
# /home·/Users 는 여기에 두지 않는다 — 홈과 홈의 상위는 is_same_or_inside
# (home, p) 규칙이, 다른 사용자 홈은 "홈의 부모 바로 아래" 규칙이 처리한다.
_POSIX_MOUNT_ROOTS = ("/Volumes", "/mnt", "/media")


def _default_forbidden_roots(home: Path) -> list[Path]:
    """금지 폴더 후보(안쪽 전체 거부) — 시스템 폴더는 환경 변수로 만든다(C: 하드코딩 금지).

    Windows 시스템 폴더는 %SystemRoot%·%ProgramFiles%·%ProgramFiles(x86)%·
    %ProgramData%·%LOCALAPPDATA%·%APPDATA% 에서 읽는다. 환경 변수가 없는
    플랫폼(macOS/Linux)에서는 대응 후보가 비는 대신 ~/AppData 같은 Windows
    관례 폴더와 POSIX 시스템 폴더를 후보로 둔다. 후보 안쪽 전체를 거부해도
    되는 폴더만 여기에 둔다 — 마운트 루트·홈의 부모처럼 "바로 아래 한 단계까지만"
    거부하는 규칙은 _is_forbidden_root 이 직접 다룬다.
    """
    candidates: list[Path] = [home / "AppData", home / "Library", Path("/System"), Path("/Library")]
    for var in (
        "SystemRoot",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramData",
        "LOCALAPPDATA",
        "APPDATA",
    ):
        value = os.environ.get(var, "")
        if value:
            candidates.append(Path(value))
    return candidates


# 금지 폴더 팩토리 — conftest 가 교체한다(Windows CI의 tmp_path 는
# %LOCALAPPDATA%\\Temp 안이라 기본 규칙에 걸린다). 제품 규칙 자체는
# _default_forbidden_roots 를 직접 검증하는 테스트가 지킨다.
_forbidden_roots_factory = _default_forbidden_roots


def _is_root_or_direct_child(p: Path, base: Path) -> bool:
    """p 가 base 자신이거나 base 바로 아래 한 단계인가(더 깊으면 거짓).

    Windows 대소문자·구분자 차이는 path_key 로 흡수한다.
    """
    pk, bk = path_key(p), path_key(base)
    if pk == bk:
        return True
    if not pk.startswith(bk + os.sep):
        return False
    rest = pk[len(bk) + len(os.sep) :]
    return bool(rest) and os.sep not in rest


def _is_forbidden_root(p: Path, home: Path) -> bool:
    """드라이브/파일시스템 루트, 홈과 홈의 모든 상위 폴더, 다른 사용자 홈,
    마운트 루트(자체와 바로 아래 한 단계), 시스템 폴더는 등록할 수 없다."""
    if p == p.parent:  # 루트 (POSIX "/", Windows "C:\\")
        return True
    if is_same_or_inside(home, p):  # p가 홈 자신이거나 홈을 포함(C:\\Users, /Users, /home)
        return True
    # 홈의 부모 바로 아래 한 단계 = 다른 사용자의 홈 전체(/Users/other 등).
    # 홈의 부모가 파일시스템 루트면(/root 홈) 루트 아래 전부를 금지하는 꼴이
    # 되므로 이 규칙은 건너뛴다. 본인 홈 아래(~/문서)는 이 규칙에 걸리지 않는다.
    if home.parent != home.parent.parent and _is_root_or_direct_child(p, home.parent):
        return True
    # 마운트 루트는 자체와 바로 아래 한 단계(드라이브 전체)만 거부한다.
    for mount in _POSIX_MOUNT_ROOTS:
        if _is_root_or_direct_child(p, Path(mount)):
            return True
    for cand in _forbidden_roots_factory(home):
        if is_same_or_inside(p, cand):
            return True
    return False


def _sensitive_matches(path: Path) -> list[str]:
    """경로 어느 부분과도 겹치는 민감 이름."""
    lowered = str(path).lower()
    return [part for part in SENSITIVE_NAME_PARTS if part in lowered]


def _next_source_id(existing: list[dict]) -> str:
    """기존 id 최대 번호 + 1(H6) — 삭제된 번호를 재사용해 중복을 만들지 않는다."""
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"src-{today}-"
    max_seq = 0
    for s in existing:
        sid = str(s.get("id", ""))
        if sid.startswith(prefix):
            try:
                max_seq = max(max_seq, int(sid[len(prefix) :]))
            except ValueError:
                continue
    return f"{prefix}{max_seq + 1:03d}"


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

    # 파일시스템 접근 전 — UNC·장치 경로는 문자열 단계에서 거부한다(S4)
    reject_remote_source_path(path)
    raw = Path(str(path)).expanduser()
    if not raw.exists():
        raise KBError("source_not_found", f"경로가 없다: {raw}")
    # 심볼릭 링크는 해석한 실제 경로로 판정한다
    resolved = raw.resolve()
    reject_remote_source_path(resolved)  # resolve 뒤에도 같은 검사
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

    # 쓰기는 공유 잠금 안에서(H7) — 겹침 검사도 잠금 안에서 읽은 목록 기준.
    with kb_lock(kb):
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
    with kb_lock(kb):  # add_source·스캔과 같은 직렬화 잠금(H7)
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
