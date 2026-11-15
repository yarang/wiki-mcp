"""Wiki 노트 쓰기 로직 — 안전장치 포함 (백업, 검증, dry-run).

순수 헬퍼 함수들 — MCP tool이 아님. server.py의 tool 래퍼가 호출함.
모든 함수는 사용자 친화적 에러 dict를 반환 (예외 발생 X).
"""

from __future__ import annotations

import re
import shutil
from datetime import date, datetime
from difflib import get_close_matches
from pathlib import Path
from typing import Any

import frontmatter

# @MX:NOTE: [AUTO] ID validation regex — kebab-case, ASCII lowercase + digits + hyphen
_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_HANGUL_RE = re.compile(r"[가-힯]")
# 도메인(폴더) 세그먼트 — 소문자/숫자 시작, [a-z0-9_-]만 허용
_DOMAIN_SEGMENT_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
# Windows 예약 장치명 — 세그먼트가 이 이름이면(확장자가 붙어도) 장치로 해석될
# 수 있다. 쓰기가 조용히 사라지거나 이상한 경로로 흘러가므로 문자열 단계에서
# 거부한다(concepts/nul.md 같은 경로를 미리 막는다).
_WINDOWS_RESERVED_RE = re.compile(r"con|prn|aux|nul|com[1-9]|lpt[1-9]")
# 도메인 전체 길이·깊이 상한 — 검증 없는 긴 도메인은 초안 저장을 멈추고
# 승인을 File name too long 으로 죽게 한다(0.4.0 보안 리뷰 1).
MAX_DOMAIN_CHARS = 100
MAX_DOMAIN_SEGMENTS = 5


def _reject_windows_reserved(name: str, *, what: str) -> None:
    stem = name.split(".")[0]  # 확장자가 붙어도('con.md') 장치로 읽힌다
    if _WINDOWS_RESERVED_RE.fullmatch(stem):
        raise ValueError(f"{what}에 Windows 예약 장치명을 쓸 수 없다: '{name}'")


def validate_domain(domain: str) -> str:
    """도메인 경로 검증 — 각 세그먼트가 [a-z0-9][a-z0-9_-]* 이어야 한다.

    `..`·절대 경로·드라이브 문자·한글 도메인을 모두 이 규칙이 거부한다.
    여러 세그먼트는 '/'로 잇는다(a/b → a/b). 전체 100자·세그먼트 5개,
    Windows 예약 장치명도 거부한다. 실패 시 ValueError.
    """
    normalized = (domain or "").strip()
    if len(normalized) > MAX_DOMAIN_CHARS:
        raise ValueError(f"도메인이 너무 길다(최대 {MAX_DOMAIN_CHARS}자, 입력 {len(normalized)}자)")
    if "\\" in normalized:  # 백슬래시 세그먼트(Windows·UNC 표기)는 허용하지 않는다
        raise ValueError(f"도메인 형식 오류 (domain: '{domain}'). 폴더 구분은 '/'만 쓴다")
    # 절대 경로('/tmp/...', '\\\\srv\\...')와 Windows 드라이브 문자('C:/...')는
    # 문자열 단계에서 거부한다. 세그먼트 규칙만으로는 '/tmp/pytest-of-runner'
    # 처럼 전부 소문자/하이픈/밑줄로 이루어진 절대 경로를 걸러내지 못한다.
    if normalized.startswith(("/", "\\")):
        raise ValueError(f"도메인은 상대 경로만 허용한다 (domain: '{domain}')")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"도메인에 드라이브 문자를 쓸 수 없다 (domain: '{domain}')")
    normalized = normalized.strip("/")
    if not normalized:
        raise ValueError("도메인이 비어 있음")
    segments = [s for s in normalized.split("/") if s]
    if not segments:
        raise ValueError("도메인이 비어 있음")
    if len(segments) > MAX_DOMAIN_SEGMENTS:
        raise ValueError(
            f"도메인 깊이가 너무 깊다(최대 {MAX_DOMAIN_SEGMENTS}세그먼트, 입력 {len(segments)}개)"
        )
    for seg in segments:
        if not _DOMAIN_SEGMENT_RE.fullmatch(seg):
            raise ValueError(
                f"도메인 형식 오류 (domain: '{domain}'). "
                f"규칙: 세그먼트마다 소문자/숫자 시작, [a-z0-9_-]만 허용 (예: 'research', 'ml/rag')"
            )
        _reject_windows_reserved(seg, what="도메인")
    return "/".join(segments)


def safe_wiki_path(wiki_root: Path, rel: str | Path) -> Path:
    """wiki 루트 안의 경로만 돌려준다 — 경로 이탈 방지 공통 헬퍼 (S2).

    MCP 도구 입력은 LLM이 만든다(원본 문서 속 지시문에 오염될 수 있다).
    `..` 상승, 절대 경로, 드라이브 문자(C:\\), UNC(\\\\server)를 거부하고
    최종 resolve() 결과가 wiki 루트 안인지 확인한다. 위반 시 ValueError.
    """
    s = str(rel)
    if s.startswith(("\\\\", "//")):
        raise ValueError(f"wiki 밖 경로는 쓸 수 없다 (네트워크 경로): {s!r}")
    if s.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", s):
        raise ValueError(f"wiki 밖 경로는 쓸 수 없다 (절대 경로): {s!r}")
    root = Path(wiki_root).resolve()
    resolved = (root / s).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"wiki 밖 경로는 쓸 수 없다: {resolved}")
    return resolved


def validate_note_id(id_str: str) -> str:
    """노트 ID 검증 — kebab-case 강제.

    규칙:
        - 패턴: ^[a-z0-9][a-z0-9-]*$ (소문자/숫자/하이픈)
        - 길이: 1-80자
        - 한글 거부 (로마자화 권장)

    Returns:
        정규화된 (소문자) ID

    Raises:
        ValueError: 규칙 위반 시
    """
    if not id_str or not isinstance(id_str, str):
        raise ValueError("ID가 비어있음")
    # kebab-case enforced as-is (no auto-lowercasing — uppercase rejected by regex)
    normalized = id_str.strip()
    if _HANGUL_RE.search(normalized):
        raise ValueError(
            f"ID에 한글 불가 (ID: '{id_str}'). 로마자화 권장 "
            f"(예: '프롬프트-엔지니어링' → 'prompt-engineering')"
        )
    if not _ID_RE.fullmatch(normalized):
        raise ValueError(
            f"ID 형식 오류 (ID: '{id_str}'). "
            f"규칙: 소문자/숫자 시작, [a-z0-9-]만 허용, 하이픈으로 단어 구분"
        )
    if len(normalized) > 80:
        raise ValueError(f"ID 길이 초과 (최대 80자, 입력 {len(normalized)}자)")
    _reject_windows_reserved(normalized, what="ID")
    return normalized


# 표준 frontmatter 필드 — 템플릿의 extra 필드 판별에 사용
STANDARD_FM_KEYS = frozenset(
    {
        "id",
        "title",
        "source_url",
        "tags",
        "created_at",
        "last_updated",
    }
)


def _yaml_quote(s: str) -> str:
    """YAML 큰따옴표 스칼라로 직렬화 — 백슬래시와 큰따옴표를 이스케이프한다.

    큰따옴표 스칼라 안의 '\\'는 이스케이프 시작이다. Windows 경로
    (C:\\Users\\...)를 그대로 두면 '\\U' 등을 YAML 이스케이프로 읽어
    파싱이 깨지고 노트가 색인에서 조용히 사라진다. 백슬래시를 먼저
    두 배로 만들어야 '\\"'가 '\\\\\"'로 뒤집히지 않는다.
    """
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_fm_value(value: Any) -> str:
    """extra frontmatter 값을 YAML 스칼라/inline-list로 직렬화."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return _yaml_quote(str(value))


def build_frontmatter(
    id: str,
    title: str,
    source_url: str,
    tags: list[str],
    created_at: str,
    last_updated: str,
    extra: dict[str, Any] | None = None,
) -> str:
    """기존 노트 스키마와 일치하는 YAML frontmatter 생성.

    포맷 (기존 노트와 동일 — inline list, quoted strings):
        ---
        id: <id>
        title: "<title>"
        source_url: "<url>"
        tags: [t1, t2, ...]
        created_at: "YYYY-MM-DD"
        last_updated: "YYYY-MM-DD"
        <extra 필드 (템플릿 정의)>
        ---

    Args:
        extra: 템플릿이 정의한 추가 필드(예: status, category). 표준 6필드는 제외됨.
    """
    safe_title = _yaml_quote(title or "")
    safe_url = _yaml_quote(source_url or "")
    tags_inline = ", ".join(tags) if tags else ""
    lines = [
        "---\n",
        f"id: {id}\n",
        f"title: {safe_title}\n",
        f"source_url: {safe_url}\n",
        f"tags: [{tags_inline}]\n",
        f'created_at: "{created_at}"\n',
        f'last_updated: "{last_updated}"\n',
    ]
    for key, value in (extra or {}).items():
        if key in STANDARD_FM_KEYS:
            continue
        lines.append(f"{key}: {_format_fm_value(value)}\n")
    lines.append("---\n")
    return "".join(lines)


def list_templates(template_dir: Path | None) -> list[str]:
    """template_dir 내 사용 가능한 템플릿 이름 목록 (파일명 stem)."""
    if not template_dir or not str(template_dir) or not template_dir.is_dir():
        return []
    return sorted(p.stem for p in template_dir.glob("*.md"))


def load_template(template_dir: Path | None, name: str) -> dict[str, Any] | None:
    """템플릿 .md 로드 → {'extra': dict, 'tags': list|None, 'body': str} 또는 None.

    템플릿 frontmatter의 표준 필드(id/title 등)는 create_note가 args로 덮어쓰므로
    extra에서 제외한다. tags는 args tags가 비었을 때만 쓰이는 기본값으로 반환.
    """
    if not template_dir or not str(template_dir) or not name:
        return None
    path = template_dir / f"{name}.md"
    if not path.exists():
        return None
    try:
        post = frontmatter.load(path)
    except Exception:
        return None
    meta = post.metadata or {}
    extra = {k: v for k, v in meta.items() if k not in STANDARD_FM_KEYS}
    return {"extra": extra, "tags": meta.get("tags"), "body": post.content}


def backup_file(path: Path) -> Path | None:
    """파일을 <name>.bak.<YYYYMMDD-HHMMSS>로 복사.

    소스가 없으면 None 반환 (조용히 스킵 — create 시 정상 케이스).
    같은 초에 두 번 백업하면 -1, -2 … 접미로 덮어쓰지 않게 한다.
    """
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak.{ts}")
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak.{ts}-{n}")
        n += 1
    shutil.copy2(path, backup)
    return backup


_HEADING_RE = re.compile(r"^(#{2,3})\s+(.*\S)\s*$")


def _fill_sections(scaffold: str, sections: dict[str, str]) -> str:
    """템플릿 스캐폴드의 각 `## 제목` 아래 본문을 sections[제목]으로 교체.

    sections에 없는 제목의 블록은 그대로 둔다({{links}} 등 보존).
    """
    lines = scaffold.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        out.append(line)
        m = _HEADING_RE.match(line)
        if m:
            heading = m.group(2).strip()
            content = sections.get(heading)
            if content is not None:
                # 다음 헤딩 직전까지 원래 블록을 건너뛰고 새 내용으로 대체
                j = i + 1
                while j < n and not _HEADING_RE.match(lines[j]):
                    j += 1
                out.append("")
                out.append(str(content).strip())
                out.append("")
                i = j
                continue
        i += 1
    return "\n".join(out)


def _build_note_path(domain: str, clean_id: str, subtype: str = "concepts") -> str:
    """`<domain>/<subtype>/<id>.md` 상대경로 생성 — 세그먼트 중복 방지.

    domain이 이미 subtype로 끝나면 subtype를 중복 추가하지 않는다
    (예: domain="concepts" → "concepts/<id>.md", "concepts/concepts/" 방지).
    """
    parts = [p for p in (domain or "").strip().strip("/").split("/") if p]
    if not parts or parts[-1] != subtype:
        parts.append(subtype)
    parts.append(f"{clean_id}.md")
    return "/".join(parts)


def _resolve_path_on_disk(idx, note_id: str) -> Path | None:
    """idx.notes에서 note_id의 상대경로로 절대경로 반환 — wiki 밖이면 None(S2).

    색인된 노트가 심볼릭 링크로 wiki 밖을 가리킬 수 있으니 resolve 로 확인한다.
    """
    note = idx.notes.get(note_id)
    if not note:
        return None
    try:
        return safe_wiki_path(idx.wiki_path, note["path"])
    except ValueError:
        return None


def _similar_ids(idx, query_id: str, limit: int = 5) -> list[str]:
    """기존 ID 중 query_id와 유사한 것들 반환 (fuzzy + substring).

    Levenshtein 기반(difflib) + substring 포함 — 오타/중복 감지용.
    """
    all_ids = list(idx.all_ids)
    if not all_ids:
        return []
    fuzzy = get_close_matches(query_id, all_ids, n=limit, cutoff=0.6)
    substring = [nid for nid in all_ids if query_id in nid or nid in query_id]
    # merge unique preserving order
    seen: set[str] = set()
    result: list[str] = []
    for nid in fuzzy + substring:
        if nid not in seen:
            seen.add(nid)
            result.append(nid)
    return result[:limit]


# @MX:WARN: [AUTO] create_note — 파일 시스템 mutation (새 노트 생성 + 인덱스 재빌드)
# @MX:REASON: 잘못된 ID/경로 생성 시 wiki 무결성 훼손; force=False에서 충돌 시 거부
def create_note(
    idx,
    graph,
    id: str,
    title: str,
    body: str,
    tags: list[str],
    domain: str | None = None,
    links: list[str] | None = None,
    source_url: str = "",
    force: bool = False,
    dry_run: bool = False,
    template: str | None = None,
    template_dir: Path | None = None,
    sections: dict[str, str] | None = None,
) -> dict[str, Any]:
    """새 wiki 노트 생성 (안전장치: ID 검증, 충돌 거부, 유사 ID 경고, dry-run).

    template 지정 시 template_dir/<template>.md 의 frontmatter(extra 필드)와
    본문 섹션 구조를 적용한다. 본문 플레이스홀더: {{body}} {{links}} {{id}}
    {{title}} {{date}} {{tags}}.

    sections 전달 시(템플릿과 함께) 템플릿의 각 `## 제목` 아래 본문을
    sections[제목]으로 채운다(예: ingestion에서 LLM이 섹션별로 작성한 경우).

    Returns:
        dict — status ∈ {"ok", "error", "preview"} + path/warnings/backup/id
    """
    # 1. ID 검증
    try:
        clean_id = validate_note_id(id)
    except ValueError as e:
        return {"status": "error", "error": str(e), "id": id}

    links = links or []
    tags = tags or []
    warnings: list[str] = []

    # 2. 충돌 검사
    if clean_id in idx.all_ids:
        if not force:
            existing_path = idx.notes.get(clean_id, {}).get("path", "?")
            return {
                "status": "error",
                "error": (
                    f"ID 충돌 — [[{clean_id}]] 이미 존재 "
                    f"(경로: wiki/{existing_path}). force=True 시 덮어쓰기."
                ),
                "id": clean_id,
                "existing_path": existing_path,
            }
        warnings.append(f"force=True — 기존 [[{clean_id}]] 덮어쓰기")

    # 3. 유사 ID 경고 (오타 감지)
    similar = _similar_ids(idx, clean_id)
    if similar:
        warnings.append(
            f"비슷한 기존 ID (오타 확인 권장): {', '.join(f'[[{s}]]' for s in similar)}"
        )

    # 4. 경로 결정 — 도메인은 세그먼트 규칙으로 검증하고 최종 경로가 wiki 안인지 확인한다(S2)
    try:
        if domain:
            top_domain = validate_domain(domain)
        else:
            # search_hybrid로 가장 관련 노트의 도메인 추정
            try:
                related = idx.search_hybrid(title or clean_id, top_k=3)
            except Exception:
                related = []
            if related:
                domains = [graph.domain_of(r["id"]) for r in related[:3] if r["id"] in idx.notes]
                top_domain = max(set(domains), key=domains.count) if domains else "concepts"
            else:
                top_domain = "concepts"
        rel_path = _build_note_path(top_domain, clean_id)
        abs_path = safe_wiki_path(idx.wiki_path, rel_path)
    except ValueError as e:
        return {"status": "error", "error": str(e), "id": clean_id, "domain": domain}

    # 5. 템플릿 로드 (지정 시) — frontmatter extra + 본문 스캐폴드
    today = date.today().isoformat()
    extra_fm: dict[str, Any] = {}
    scaffold: str | None = None
    if template:
        tmpl = load_template(template_dir, template)
        if tmpl is None:
            avail = list_templates(template_dir)
            warnings.append(
                f"템플릿 '{template}' 없음 (사용 가능: {avail or '없음'}). 템플릿 없이 생성."
            )
        else:
            extra_fm = tmpl["extra"]
            if not tags and tmpl.get("tags"):
                t = tmpl["tags"]
                tags = list(t) if isinstance(t, (list, tuple)) else [t]
            scaffold = tmpl["body"]

    # 6. 본문 구성
    fm = build_frontmatter(
        id=clean_id,
        title=title or clean_id,
        source_url=source_url,
        tags=tags,
        created_at=today,
        last_updated=today,
        extra=extra_fm,
    )

    # 링크 bullet — display는 대상 노트 title 사용
    link_lines: list[str] = []
    for target in links:
        target_clean = target.strip().strip("[]")
        if target_clean in idx.notes:
            display = idx.notes[target_clean].get("title", target_clean)
            link_lines.append(f"- [[{target_clean}|{display}]]")
        else:
            warnings.append(f"링크 대상 [[{target_clean}]] 현재 wiki에 없음 (broken link 됨)")
            link_lines.append(f"- [[{target_clean}]]")
    link_bullets = "\n".join(link_lines)

    # 본문 = 템플릿 스캐폴드(있으면) 또는 자유 body
    uses_placeholder_links = scaffold is not None and "{{links}}" in scaffold
    if scaffold is not None:
        note_body = scaffold
        # 섹션별 내용이 주어지면 각 `## 제목` 아래를 채움 (ingestion 등)
        if sections:
            note_body = _fill_sections(note_body, sections)
        if "{{body}}" in note_body:
            note_body = note_body.replace("{{body}}", (body or "").strip())
        elif body and not sections:
            note_body = body.rstrip() + "\n\n" + note_body.lstrip()
        if uses_placeholder_links:
            note_body = note_body.replace(
                "{{links}}", link_bullets if link_bullets else "_(아직 없음)_"
            )
    else:
        note_body = body or ""

    content = fm + "\n" + note_body
    # 템플릿이 {{links}}로 링크를 직접 배치하지 않은 경우에만 자동 섹션 추가
    if not uses_placeholder_links and link_lines:
        content += "\n\n## 관련 노트\n\n" + link_bullets + "\n"

    # 플레이스홀더 치환 (frontmatter extra 값 + 본문 공통)
    content = (
        content.replace("{{id}}", clean_id)
        .replace("{{title}}", title or clean_id)
        .replace("{{date}}", today)
        .replace("{{tags}}", ", ".join(tags))
    )

    # 6. dry-run: 미리보기 반환
    if dry_run:
        return {
            "status": "preview",
            "id": clean_id,
            "path": str(abs_path),
            "relative_path": rel_path,
            "warnings": warnings,
            "content_preview": (content[:1000] + "..." if len(content) > 1000 else content),
        }

    # 7. 실제 쓰기
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if abs_path.exists() and force:
            backup = backup_file(abs_path)
            if backup:
                warnings.append(f"기존 파일 백업: {backup}")
        abs_path.write_text(content, encoding="utf-8")
        idx.build()
    except Exception as e:
        return {
            "status": "error",
            "error": f"쓰기 실패: {e}",
            "id": clean_id,
            "path": str(abs_path),
        }

    return {
        "status": "ok",
        "id": clean_id,
        "path": str(abs_path),
        "relative_path": rel_path,
        "warnings": warnings,
        "backup": str(backup) if backup else None,
    }


def update_note(
    idx,
    id: str,
    body: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    append: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """기존 노트 수정 (자동 .bak 백업, last_updated 자동 갱신).

    body/title/tags가 None이면 해당 필드 유지. append=True 시 body를 기존 본문 끝에 추가.
    """
    clean_id = id.strip().strip("[]")

    # 1. 파일 위치 확인
    if clean_id not in idx.notes:
        candidates = _similar_ids(idx, clean_id)
        return {
            "status": "error",
            "error": f"노트 [[{clean_id}]] 없음",
            "suggestions": candidates,
        }
    abs_path = _resolve_path_on_disk(idx, clean_id)
    if abs_path is None:
        return {
            "status": "error",
            "error": f"노트 [[{clean_id}]]의 경로가 wiki 밖을 가리킨다 — 수정 거부",
        }
    if not abs_path.exists():
        return {
            "status": "error",
            "error": (f"노트 [[{clean_id}]] 인덱스에 있으나 파일 없음 (path: {abs_path})"),
        }

    # 2. frontmatter 파싱
    try:
        post = frontmatter.load(abs_path)
    except Exception as e:
        return {
            "status": "error",
            "error": f"frontmatter 파싱 실패: {e}",
            "id": clean_id,
            "path": str(abs_path),
        }

    # 3. 메타 병합
    new_title = title if title is not None else (post.get("title", "") or "")
    current_tags = post.get("tags", []) or []
    new_tags = tags if tags is not None else current_tags
    if isinstance(new_tags, str):
        new_tags = [new_tags]
    new_source = post.get("source_url", "") or ""
    new_created = post.get("created_at", "") or date.today().isoformat()
    today = date.today().isoformat()

    # 4. 본문 처리
    current_content = post.content
    if body is not None:
        if append:
            new_body = current_content.rstrip() + "\n\n" + body
        else:
            new_body = body
    else:
        new_body = current_content

    # 5. 재조립 (build_frontmatter로 포맷 일관성 보존)
    fm = build_frontmatter(
        id=clean_id,
        title=new_title,
        source_url=new_source,
        tags=new_tags,
        created_at=str(new_created),
        last_updated=today,
    )
    new_content = fm + "\n" + new_body.rstrip() + "\n"

    changes = {
        "title_changed": title is not None,
        "tags_changed": tags is not None,
        "body_changed": body is not None,
        "append_mode": append and body is not None,
        "last_updated": today,
    }

    # 6. dry-run
    if dry_run:
        return {
            "status": "preview",
            "id": clean_id,
            "path": str(abs_path),
            "changes": changes,
            "content_preview": (
                new_content[:1000] + "..." if len(new_content) > 1000 else new_content
            ),
        }

    # 7. 백업 + 쓰기
    try:
        backup = backup_file(abs_path)
        abs_path.write_text(new_content, encoding="utf-8")
        idx.build()
    except Exception as e:
        return {
            "status": "error",
            "error": f"수정 실패: {e}",
            "id": clean_id,
            "path": str(abs_path),
        }

    return {
        "status": "ok",
        "id": clean_id,
        "path": str(abs_path),
        "backup": str(backup) if backup else None,
        "changes": changes,
    }


def add_link(
    idx,
    id: str,
    target_id: str,
    display: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """소스 노트의 '## 관련 노트' 섹션에 [[target_id|display]] 링크 추가 (단방향).

    양방향 링크 원할 시 호출자가 add_link를 양쪽으로 두 번 호출 (A→B, B→A).
    대상이 없으면 거부 (broken link 사전 방지).
    """
    src_id = id.strip().strip("[]")
    tgt_id = target_id.strip().strip("[]")

    # 1. 양쪽 ID 존재 확인
    if src_id not in idx.notes:
        return {"status": "error", "error": f"소스 노트 [[{src_id}]] 없음"}
    if tgt_id not in idx.notes:
        suggestions = _similar_ids(idx, tgt_id)
        return {
            "status": "error",
            "error": (f"대상 노트 [[{tgt_id}]] 없음 — 링크 생성 거부 (broken link 사전 방지)"),
            "suggestions": suggestions,
        }

    abs_path = _resolve_path_on_disk(idx, src_id)
    if abs_path is None:
        return {
            "status": "error",
            "error": f"노트 [[{src_id}]]의 경로가 wiki 밖을 가리킨다 — 링크 추가 거부",
        }
    if not abs_path.exists():
        return {"status": "error", "error": f"소스 파일 없음: {abs_path}"}

    # 2. display 결정
    if not display:
        display = idx.notes[tgt_id].get("title", tgt_id)
    link_line = f"- [[{tgt_id}|{display}]]"

    # 3. 읽기
    try:
        text = abs_path.read_text(encoding="utf-8")
    except Exception as e:
        return {"status": "error", "error": f"읽기 실패: {e}"}

    # 4. 이미 링크 존재 여부
    if f"[[{tgt_id}" in text:
        return {
            "status": "ok",
            "id": src_id,
            "path": str(abs_path),
            "message": f"링크 [[{tgt_id}]] 이미 존재 — 변경 없음",
            "already_present": True,
        }

    # 5. 섹션 탐지 및 삽입
    section_header = "## 관련 노트"
    section_created = section_header not in text

    if not section_created:
        # 기존 섹션에 추가 — 다음 ## 헤더 직전 또는 EOF
        idx_section = text.index(section_header)
        after_header = idx_section + len(section_header)
        next_header_match = re.search(r"\n## ", text[after_header:])
        if next_header_match:
            insert_offset = after_header + next_header_match.start()
            new_text = text[:insert_offset] + link_line + "\n" + text[insert_offset:]
        else:
            new_text = text.rstrip() + "\n" + link_line + "\n"
    else:
        # 섹션 없음 — 끝에 새 섹션 추가
        new_text = text.rstrip() + "\n\n" + section_header + "\n\n" + link_line + "\n"

    if dry_run:
        return {
            "status": "preview",
            "id": src_id,
            "path": str(abs_path),
            "link_added": link_line,
            "section_created": section_created,
        }

    # 6. 백업 + 쓰기
    try:
        backup = backup_file(abs_path)
        abs_path.write_text(new_text, encoding="utf-8")
        idx.build()
    except Exception as e:
        return {"status": "error", "error": f"링크 추가 실패: {e}"}

    return {
        "status": "ok",
        "id": src_id,
        "path": str(abs_path),
        "link_added": link_line,
        "backup": str(backup) if backup else None,
        "section_created": section_created,
    }


# @MX:WARN: [AUTO] delete_note — 파괴적 연산 (파일 영구 삭제). confirm=True 필수
# @MX:REASON: 삭제 시 wiki 무결성 훼손 + inbound link broken; 백업은 하되 복구 수동
def delete_note(
    idx,
    id: str,
    confirm: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """노트 삭제 (가장 안전한 도구). confirm=True 필수, 자동 백업.

    삭제 후 inbound link가 broken 될 노트 ID 목록을 반환하여 후속 정치 유도.
    """
    clean_id = id.strip().strip("[]")

    # 1. confirm 게이트
    if not confirm:
        return {
            "status": "error",
            "error": ("confirmation required — confirm=True 설정 필요 (삭제는 되돌릴 수 없음)"),
            "id": clean_id,
            "hint": "백업은 자동 생성되지만, dry_run=True로 미리보기 먼저 권장",
        }

    # 2. 파일 위치
    if clean_id not in idx.notes:
        return {
            "status": "error",
            "error": f"노트 [[{clean_id}]] 없음 (이미 삭제됨?)",
        }
    abs_path = _resolve_path_on_disk(idx, clean_id)
    if abs_path is None:
        return {
            "status": "error",
            "error": f"노트 [[{clean_id}]]의 경로가 wiki 밖을 가리킨다 — 삭제 거부",
        }
    if not abs_path.exists():
        return {
            "status": "error",
            "error": (f"노트 [[{clean_id}]] 인덱스에 있으나 파일 없음: {abs_path}"),
        }

    # 3. inbound link 사전 캡처 (삭제 후 broken 될 소스들)
    inbound = sorted(idx.in_links.get(clean_id, set()))

    # 4. dry-run
    if dry_run:
        return {
            "status": "preview",
            "id": clean_id,
            "path": str(abs_path),
            "inbound_links_to_break": inbound,
            "message": (
                f"삭제 시 {len(inbound)}개 노트의 링크가 broken 됨: "
                + (", ".join(f"[[{n}]]" for n in inbound) if inbound else "없음")
            ),
        }

    # 5. 백업 + 삭제
    try:
        backup = backup_file(abs_path)
        abs_path.unlink()
        idx.build()  # ChromaDB cleanup 자동 수행
    except Exception as e:
        return {
            "status": "error",
            "error": f"삭제 실패: {e}",
            "id": clean_id,
            "path": str(abs_path),
        }

    if inbound:
        message = (
            f"삭제 완료. 백업: {backup}. "
            f"주의: {len(inbound)}개 노트가 여전히 [[{clean_id}]] 참조 (broken) — "
            f"add_link로 대안 노트 연결 권장"
        )
    else:
        message = f"삭제 완료. 백업: {backup}. inbound link 없음."

    return {
        "status": "ok",
        "id": clean_id,
        "path": str(abs_path),
        "backup": str(backup) if backup else None,
        "inbound_links_broken": inbound,
        "message": message,
    }
