"""KB 노트 파일 연산 — 검토 대기열과 mark_orphans 가 함께 쓰는 내부 공용 모듈.

writer.create_note/update_note 는 표준 6필드 밖의 머리말 키를 버리므로 재사용하지
않는다(설계 2.6절). 여기서는 머리말을 **키 단위로 편집**하고 사후 safe_load 비교로
다른 키가 바뀌지 않았음을 확인한다(YAML 주입·편집기 재작성 방어).
"""

from __future__ import annotations

import hashlib
import re

import frontmatter
import yaml

from wiki_mcp.kb.errors import KBError
from wiki_mcp.writer import safe_wiki_path

_FM_KEY_LINE_RE = "^{key}:"


def find_notes(wiki_dir) -> dict[str, list[str]]:
    """id → posix 상대 경로 목록. indexer 와 같은 노트 스캔 규칙.

    rglob("*.md") 을 정렬해 돌고, 숨김 세그먼트가 있으면 제외하며,
    frontmatter 파싱 실패·id 없음은 건너뛴다. 경로는 safe_wiki_path 로 다시
    확인해 wiki 밖을 가리키면 제외한다.
    """
    from pathlib import Path

    root = Path(wiki_dir)
    notes: dict[str, list[str]] = {}
    if not root.is_dir():
        return notes
    for md in sorted(root.rglob("*.md")):
        try:
            rel_parts = md.relative_to(root).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel_parts):
            continue
        try:
            post = frontmatter.load(md)
        except Exception:
            continue
        note_id = post.get("id")
        if not note_id:
            continue
        try:
            if safe_wiki_path(root, md.relative_to(root)) != md.resolve():
                continue
        except (ValueError, OSError):
            continue
        notes.setdefault(str(note_id), []).append(md.relative_to(root).as_posix())
    return notes


def _split_frontmatter(text: str) -> tuple[list[str], int, int]:
    """머리말 블록 경계를 찾는다. (줄 목록, 여는 줄 index, 닫는 줄 index).

    텍스트는 LF 로 정규화한 상태여야 한다. 첫 줄이 정확히 '---' 이고 그 뒤
    처음으로 rstrip() 결과가 '---' 인 줄이 닫는 줄이다. 아니면
    note_frontmatter_unsupported.
    """
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        raise KBError("note_frontmatter_unsupported", "노트 머리말이 없거나 여는 --- 이 아니다")
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return lines, 0, i
    raise KBError("note_frontmatter_unsupported", "노트 머리말의 닫는 --- 을 찾지 못했다")


def _key_block_range(lines: list[str], start: int, close: int, key: str) -> tuple[int, int] | None:
    """머리말 안의 키 블록 범위 [begin, end). 블록은 ^{key}: 줄과 그 뒤로 이어지는
    들여쓰기·'- ' 줄들이다(블록 리스트·접힌 값 재작성 대비 — R2). 블록 뒤의 빈
    줄은 키에 속하지 않는다 — 키 줄을 빼도 나머지 줄이 그대로 남아야 한다."""
    pattern = re.compile(_FM_KEY_LINE_RE.format(key=re.escape(key)))
    begin = None
    for i in range(start + 1, close):
        if pattern.match(lines[i]):
            begin = i
            break
    if begin is None:
        return None
    end = begin + 1
    while end < close:
        line = lines[end]
        if line.startswith((" ", "\t")) or line.lstrip().startswith("- "):
            end += 1
        else:
            break
    return begin, end


def preserve_newlines(original: str, rewritten: str) -> str:
    """rewritten(LF 정규화본)을 original 의 줄끝 규칙에 맞춘다.

    original 이 CRLF 만 쓰면 CRLF 로 되돌리고, 그 밖에는(LF·혼재)
    rewritten 을 그대로 둔다. 노트 편집(set_frontmatter_key 등)은 LF 로
    정규화해 다루므로, 쓰기 직전에 원래 줄끝을 복원한다.
    """
    lf_count = original.count("\n")
    if lf_count and original.count("\r\n") == lf_count:  # 순수 CRLF
        return rewritten.replace("\n", "\r\n")
    return rewritten


def set_frontmatter_key(text: str, key: str, value_line: str | None) -> str:
    """머리말의 한 키를 교체·추가(value_line)하거나 지운다(None).

    사후 검증: 원래 머리말과 새 머리말을 safe_load 해 바꾼 키를 뺀 나머지가
    같아야 하고, 새 값이 의도한 값으로 다시 읽혀야 한다. 어긋나면
    note_frontmatter_unsupported 이고 원문을 그대로 돌려준 채 예외를 던진다.
    """
    normalized = text.replace("\r\n", "\n")
    lines, open_idx, close_idx = _split_frontmatter(normalized)
    try:
        before = yaml.safe_load("\n".join(lines[open_idx + 1 : close_idx])) or {}
    except yaml.YAMLError as e:
        raise KBError("note_frontmatter_unsupported", f"노트 머리말 파싱 실패: {e}") from e
    if not isinstance(before, dict):
        raise KBError("note_frontmatter_unsupported", "노트 머리말이 매핑이 아니다")

    out = list(lines)
    block = _key_block_range(out, open_idx, close_idx, key)
    if block is not None:
        begin, end = block
        del out[begin:end]
        close_idx -= end - begin
    if value_line is not None:
        out.insert(close_idx, f"{key}: {value_line}")

    parse_end = close_idx + 1 if value_line is not None else close_idx
    try:
        after = yaml.safe_load("\n".join(out[open_idx + 1 : parse_end])) or {}
        expected = None
        if value_line is not None:
            expected = yaml.safe_load(f"{key}: {value_line}").get(key)
    except yaml.YAMLError as e:
        raise KBError("note_frontmatter_unsupported", f"편집 뒤 머리말 파싱 실패: {e}") from e
    if not isinstance(after, dict):
        raise KBError("note_frontmatter_unsupported", "편집 뒤 머리말이 매핑이 아니다")
    before_wo_key = {k: v for k, v in before.items() if k != key}
    after_wo_key = {k: v for k, v in after.items() if k != key}
    if before_wo_key != after_wo_key:
        raise KBError(
            "note_frontmatter_unsupported",
            f"머리말 편집이 다른 키를 바꿨다: {key}",
        )
    if value_line is not None and after.get(key) != expected:
        raise KBError(
            "note_frontmatter_unsupported",
            f"머리말 값이 왕복하지 않는다: {key}",
        )
    return "\n".join(out)


def replace_note_body(text: str, new_body: str) -> str:
    """머리말은 바이트 그대로 두고 본문만 교체한다."""
    normalized = text.replace("\r\n", "\n")
    lines, _open, close = _split_frontmatter(normalized)
    head = "\n".join(lines[: close + 1])
    return f"{head}\n\n{new_body}"


def note_content_hash(text: str) -> str:
    """노트 내용 해시 — LF 정규화 뒤 kb_source_status 블록을 뺀 텍스트의 sha256.

    kb_source_status 표시만 바뀐 경우는 충돌이 아니다(설계 요구 4). 머리말이
    없거나 규칙과 맞지 않으면 LF 정규화한 전체 텍스트의 해시를 쓴다.
    """
    normalized = text.replace("\r\n", "\n")
    try:
        stripped = set_frontmatter_key(normalized, "kb_source_status", None)
    except KBError:
        stripped = normalized
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()


def render_sources_section(links: list[dict]) -> str:
    """노트 출처 절 — links 항목(정렬된 순서)에서 근거마다 한 줄씩.

    화면 표시용 rel_path 의 백틱은 ' 로 바꾼다(마크다운 코드 범위 파괴 방지).
    정확한 값은 머리말 kb_sources 에 있다.
    """
    lines = ["<!-- kb:sources:start -->", "", "## 출처", ""]
    for link in links:
        rel = str(link.get("rel_path", "")).replace("`", "'")
        lines.append(f"- `{rel}` ({link.get('source_id', '')})")
        for ev in link.get("evidence", []):
            location = ev.get("location") or "위치 없음"
            lines.append(f'  - {location}: "{ev.get("quote", "")}"')
        if not link.get("evidence"):
            lines.append('  - 위치 없음: ""')
    lines.append("<!-- kb:sources:end -->")
    return "\n".join(lines) + "\n"
