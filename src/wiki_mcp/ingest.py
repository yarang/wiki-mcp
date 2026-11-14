"""raw 원문 → wiki 노트 변환 (LLM 기반).

watch 데몬과 `ingest_raw` MCP 도구가 공용으로 사용한다. LLM 호출은 `complete_fn`
콜러블로 주입받아(테스트 가능) create_note의 안전장치를 그대로 재사용한다.

흐름: raw 텍스트 → LLM이 원자 노트 JSON 생성 → create_note(template) → wiki/ 기록.
"""
from __future__ import annotations

import json
import hashlib
import re
import shutil
from pathlib import Path
from typing import Any, Callable

import requests

from wiki_mcp.writer import create_note, load_template

_HEADING_RE = re.compile(r"^#{2,3}\s+(.*\S)\s*$")

_RULES = (
    "규칙:\n"
    "- id: 소문자/숫자/하이픈만. 한글 불가(로마자화). 1-80자.\n"
    "- links: 아래 '기존 노트 ID' 목록에 있는 것만 넣는다. 관련 없으면 빈 배열.\n"
    "- tags: 주제를 나타내는 키워드 2~4개. 노트 id를 tags에 넣지 마라.\n"
    "- 하나의 주제만 담는다(원자성). 여러 주제면 가장 핵심 하나로 요약한다.\n"
    "- 원문에 있는 내용만 쓴다. 부풀리거나 지어내지 않는다.\n"
    "- evidence: 핵심 주장마다 원문에서 그대로 복사한 짧은 구절을 넣는다. 요약하거나 고쳐 쓰지 마라.\n"
    "- 본문에서 다른 노트는 [[id]] 형식으로 링크할 수 있다.\n"
)


def template_section_headings(scaffold: str) -> list[str]:
    """스캐폴드의 `## 제목` 목록. 단 {{links}}가 든 섹션(관련 노트)은 제외."""
    lines = scaffold.split("\n")
    headings: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        m = _HEADING_RE.match(lines[i])
        if not m:
            i += 1
            continue
        heading = m.group(1).strip()
        j = i + 1
        block: list[str] = []
        while j < n and not _HEADING_RE.match(lines[j]):
            block.append(lines[j])
            j += 1
        if "{{links}}" not in "\n".join(block):
            headings.append(heading)
        i = j
    return headings


def build_prompts(
    raw_text: str,
    candidate_ids: list[str],
    section_headings: list[str] | None = None,
    candidate_details: list[str] | None = None,
) -> tuple[str, str]:
    """(system, user) 프롬프트 생성. section_headings가 있으면 섹션별 작성 요청."""
    if section_headings:
        secs = ", ".join(f'"{h}"' for h in section_headings)
        schema = (
            '{"id": "kebab-case-ascii", "title": "제목", "tags": ["tag1"], '
            '"links": ["existing-note-id"], '
            f'"sections": {{{secs}: "각 섹션의 마크다운 내용"}}, '
            '"evidence": ["원문에서 그대로 복사한 근거 구절"]}'
        )
        section_note = (
            f"- sections: 아래 섹션 제목 각각에 원문 기반 내용을 채운다: {secs}. "
            "내용이 없으면 빈 문자열.\n"
        )
    else:
        schema = (
            '{"id": "kebab-case-ascii", "title": "제목", "tags": ["tag1"], '
            '"body": "마크다운 본문", "links": ["existing-note-id"], '
            '"evidence": ["원문에서 그대로 복사한 근거 구절"]}'
        )
        section_note = "- body: 핵심을 간결한 마크다운으로 작성한다.\n"

    system = (
        "너는 원문 텍스트를 하나의 원자적(atomic) 위키 노트로 변환하는 도구다. "
        "반드시 아래 JSON 객체 하나만 출력한다. 설명 문장이나 코드펜스(```)를 붙이지 마라.\n"
        f"{schema}\n{_RULES}{section_note}"
    )
    ids = "\n".join(candidate_details or candidate_ids) if candidate_ids else "(없음)"
    user = (
        f"기존 노트 ID: {ids}\n\n"
        f"=== 원문 시작 ===\n{raw_text}\n=== 원문 끝 ==="
    )
    return system, user


def related_candidates(idx, text: str, limit: int = 8) -> list[dict[str, Any]]:
    """로컬 검색으로 LLM에 줄 기존 노트 후보를 작게 제한한다.

    별도 LLM 호출 없이 기존 BM25/hybrid 인덱스를 사용한다. 검색 실패 시에도
    결정적으로 동작하도록 ID 정렬 순서의 소수 후보로 폴백한다.
    """
    limit = max(1, min(int(limit), 12))
    query = " ".join(text.split())[:2000]
    try:
        results = idx.search_hybrid(query, top_k=limit) if query else []
    except Exception:
        results = []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        nid = str(result.get("id", "")).strip()
        if not nid or nid not in idx.all_ids or nid in seen:
            continue
        note = idx.notes.get(nid, {})
        candidates.append({
            "id": nid,
            "title": str(note.get("title", result.get("title", "")) or ""),
            "tags": [str(t) for t in (note.get("tags", result.get("tags", [])) or [])],
        })
        seen.add(nid)
    if not candidates:
        for nid in sorted(idx.all_ids)[:limit]:
            note = idx.notes.get(nid, {})
            candidates.append({
                "id": nid,
                "title": str(note.get("title", "") or ""),
                "tags": [str(t) for t in (note.get("tags", []) or [])],
            })
    return candidates[:limit]


def _candidate_details(candidates: list[dict[str, Any]]) -> list[str]:
    details: list[str] = []
    for candidate in candidates:
        tags = ", ".join(candidate.get("tags", [])[:4]) or "태그 없음"
        details.append(f"- {candidate['id']} | {candidate.get('title') or candidate['id']} | {tags}")
    return details


def filter_allowed_links(links: Any, allowed_ids: set[str]) -> tuple[list[str], list[str]]:
    """로컬 모델이 후보 밖 ID를 만들어도 Wiki 링크로 반영하지 않는다."""
    accepted: list[str] = []
    rejected: list[str] = []
    for value in links or []:
        nid = str(value).strip().strip("[]")
        if not nid or nid in accepted:
            continue
        if nid in allowed_ids:
            accepted.append(nid)
        else:
            rejected.append(nid)
    return accepted, rejected


def validate_evidence(raw_text: str, evidence: Any) -> list[str]:
    """LLM 근거가 실제 원문에 존재하는지 검증한다.

    공백 차이만 정규화하고 어휘 변경은 허용하지 않는다. 이렇게 해야 LLM이
    만든 문장을 근거처럼 제출하는 것을 차단할 수 있다.
    """
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("원문 근거(evidence)가 없음")
    normalized_raw = " ".join(raw_text.split())
    verified: list[str] = []
    for value in evidence[:10]:
        quote = str(value).strip()
        normalized_quote = " ".join(quote.split())
        if len(normalized_quote) < 8:
            raise ValueError("원문 근거가 너무 짧음(최소 8자)")
        if normalized_quote not in normalized_raw:
            raise ValueError(f"원문에 존재하지 않는 근거: {quote[:80]}")
        verified.append(quote)
    return verified


def _evidence_markdown(evidence: list[str], source_id: str) -> str:
    lines = ["## 원문 근거", "", f"출처: [[{source_id}]]", ""]
    lines.extend(f"> {quote}" for quote in evidence)
    return "\n".join(lines)


def _source_id(path: Path, raw_text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")[:40]
    if not slug:
        slug = "document"
    digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:12]
    return f"source-{slug}-{digest}"


def create_source_note(idx, graph, raw_path: Path, raw_text: str) -> dict[str, Any]:
    """추출 원문 전체를 LLM 변환 전에 Wiki Source 노트로 보존한다."""
    source_id = _source_id(raw_path, raw_text)
    if source_id in idx.all_ids:
        return {
            "status": "ok",
            "id": source_id,
            "relative_path": idx.notes[source_id].get("path"),
            "existing": True,
        }
    body = (
        "## 원문 정보\n\n"
        f"- 파일명: `{raw_path.name}`\n"
        f"- SHA-256: `{hashlib.sha256(raw_text.encode('utf-8')).hexdigest()}`\n"
        "- 보존 방식: 추출 텍스트 전체\n\n"
        "## 원문\n\n"
        f"{raw_text.rstrip()}\n"
    )
    return create_note(
        idx, graph,
        id=source_id,
        title=f"원문: {raw_path.name}",
        body=body,
        tags=["source", raw_path.suffix.lower().lstrip(".") or "text"],
        domain="sources",
        source_url=str(raw_path),
    )


def parse_note_json(text: str) -> dict[str, Any]:
    """LLM 출력에서 첫 JSON 객체를 추출·파싱. 코드펜스/설명문 방어.

    Raises:
        ValueError: JSON 객체를 못 찾거나 파싱 실패 시.
    """
    s = (text or "").strip()
    s = re.sub(r"```(?:json)?", "", s).strip()  # 코드펜스 제거
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("응답에서 JSON 객체를 찾지 못함")
    try:
        return json.loads(s[start : end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 파싱 실패: {e}")


def openai_chat_complete(
    url: str,
    model: str,
    api_key: str,
    system: str,
    user: str,
    timeout: int = 120,
) -> str:
    """OpenAI 호환 chat completions 호출 → assistant 메시지 content 반환.

    Open WebUI(:8080/api/chat/completions), LM Studio, OpenAI 등 호환.
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    r = requests.post(url, json=payload, headers=headers, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def ingest_text(
    idx,
    graph,
    raw_text: str,
    *,
    complete_fn: Callable[[str, str], str],
    template: str | None = None,
    template_dir: Path | None = None,
    force: bool = False,
    source_id: str | None = None,
    require_evidence: bool = False,
) -> dict[str, Any]:
    """원문 텍스트 하나를 LLM으로 노트화하여 create_note로 기록.

    Args:
        complete_fn: (system, user) -> assistant content. LLM 호출을 주입.
    Returns:
        create_note 결과 dict (+ 실패 시 raw_llm 미리보기).
    """
    candidates = related_candidates(idx, raw_text)
    candidate_ids = [candidate["id"] for candidate in candidates]

    # 템플릿이 섹션 구조를 가지면 LLM에게 섹션별로 작성하도록 요청
    section_headings: list[str] = []
    if template:
        tmpl = load_template(template_dir, template)
        if tmpl:
            section_headings = template_section_headings(tmpl["body"])

    system, user = build_prompts(
        raw_text, candidate_ids, section_headings, _candidate_details(candidates)
    )
    llm_out = complete_fn(system, user)
    note = parse_note_json(llm_out)

    evidence: list[str] = []
    if require_evidence:
        try:
            evidence = validate_evidence(raw_text, note.get("evidence"))
        except ValueError as e:
            return {"status": "error", "error": str(e), "raw_llm": (llm_out or "")[:500]}

    nid = str(note.get("id", "")).strip()
    if not nid:
        return {
            "status": "error",
            "error": "LLM 응답에 id 없음",
            "raw_llm": (llm_out or "")[:500],
        }

    tags = [str(t) for t in (note.get("tags") or [])]
    links, rejected_links = filter_allowed_links(note.get("links"), set(candidate_ids))
    raw_sections = note.get("sections") if section_headings else None
    sections = None
    if isinstance(raw_sections, dict):
        sections = {str(k): str(v) for k, v in raw_sections.items() if v}
    body = str(note.get("body", "") or "")
    if source_id and evidence:
        evidence_md = _evidence_markdown(evidence, source_id)
        if sections:
            last = next(reversed(sections))
            sections[last] = sections[last].rstrip() + "\n\n" + evidence_md
        else:
            body = body.rstrip() + "\n\n" + evidence_md
    note_links = links + ([source_id] if source_id and source_id not in links else [])
    result = create_note(
        idx,
        graph,
        id=nid,
        title=str(note.get("title", "") or nid),
        body=body,
        tags=tags,
        links=note_links,
        source_url=f"wiki://{source_id}" if source_id else str(note.get("source_url", "") or ""),
        template=template,
        template_dir=template_dir,
        sections=sections,
        force=force,
    )
    if rejected_links:
        result.setdefault("warnings", []).append(
            "검색 후보 밖 링크 제거: " + ", ".join(rejected_links)
        )
    return result


# ─── 긴 문서 청킹 (아웃라인 → 노트별 작성) ────────────────────────────────

def split_into_windows(text: str, max_chars: int) -> list[str]:
    """마크다운 헤딩(#..######) 경계 우선 → 큰 조각은 문단 단위로 max_chars까지 묶음."""
    if max_chars <= 0 or len(text) <= max_chars:
        return [text] if text.strip() else []

    # 1) 헤딩 경계로 1차 분할 (헤딩 라인을 새 블록 시작으로)
    blocks: list[str] = []
    cur: list[str] = []
    for line in text.split("\n"):
        if re.match(r"^#{1,6}\s+\S", line) and cur:
            blocks.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))

    # 2) 큰 블록은 문단(\n\n) 단위로 재분할
    pieces: list[str] = []
    for b in blocks:
        if len(b) <= max_chars:
            pieces.append(b)
            continue
        buf = ""
        for para in re.split(r"\n\s*\n", b):
            if buf and len(buf) + len(para) + 2 > max_chars:
                pieces.append(buf)
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if buf:
            pieces.append(buf)

    # 3) 인접한 작은 조각 병합 (max_chars 이하 유지)
    windows: list[str] = []
    buf = ""
    for p in pieces:
        if buf and len(buf) + len(p) + 2 > max_chars:
            windows.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        windows.append(buf)
    return [w for w in windows if w.strip()]


def _outline_prompts(window_text: str, existing_ids: list[str], max_notes: int) -> tuple[str, str]:
    ids = ", ".join(existing_ids[:60]) if existing_ids else "(없음)"
    system = (
        "너는 긴 문서를 원자적(atomic) 위키 노트로 나누는 편집자다. "
        "독립적으로 의미 있는 개념 단위로만 계획하고, 본문은 쓰지 않는다. "
        "반드시 아래 JSON 객체 하나만 출력한다(설명·코드펜스 금지):\n"
        '{"notes": [{"id": "kebab-case-ascii", "title": "제목", "scope": "다룰 범위 1줄"}]}\n'
        "규칙:\n"
        "- id: 소문자/숫자/하이픈, 한글 불가, 서로 중복 없이.\n"
        "- 한 개념을 정의·원리·장점·예시 등으로 쪼개지 마라. 그것들은 한 노트 안의 섹션으로 다룬다.\n"
        "- 보통 문서의 소제목(## ) 하나가 노트 하나에 대응한다. 과도하게 나누지 마라.\n"
        f"- 노트는 최대 {max_notes}개. 서로 내용이 겹치는 항목은 하나로 합친다.\n"
        "- 원문에 실제로 있는 개념만 계획한다. 없는 내용을 지어내지 마라.\n"
        "- 아래 '기존 노트 ID'와 같은 개념이면 새로 만들지 않는다.\n"
    )
    user = f"기존 노트 ID: {ids}\n\n=== 문서 조각 ===\n{window_text}\n=== 끝 ==="
    return system, user


def outline_document(
    raw_text: str,
    existing_ids: list[str],
    complete_fn: Callable[[str, str], str],
    max_chars: int,
    max_notes: int,
) -> tuple[list[dict], list[str]]:
    """문서를 창으로 나눠 창마다 노트 계획을 수집·병합. (planned, windows) 반환."""
    windows = split_into_windows(raw_text, max_chars) or [raw_text]
    planned: list[dict] = []
    seen: set[str] = set()
    for wi, w in enumerate(windows):
        if len(planned) >= max_notes:
            break
        # 헤딩만 있고 내용이 거의 없는 창은 아웃라인 호출 낭비 → 건너뜀
        body_only = "\n".join(ln for ln in w.split("\n") if not re.match(r"^#{1,6}\s", ln))
        if len(body_only.strip()) < 30:
            continue
        try:
            data = parse_note_json(complete_fn(*_outline_prompts(w, existing_ids, max_notes)))
        except Exception:
            continue
        for n in (data.get("notes") or []):
            nid = str(n.get("id", "")).strip()
            if not nid or nid in seen:
                continue
            seen.add(nid)
            planned.append({
                "id": nid,
                "title": str(n.get("title", "") or nid),
                "scope": str(n.get("scope", "") or ""),
                "window_idx": wi,
            })
            if len(planned) >= max_notes:
                break
    return planned, windows


def _write_prompts(
    window_text: str,
    planned: dict,
    sibling_ids: list[str],
    section_headings: list[str],
    candidate_details: list[str] | None = None,
) -> tuple[str, str]:
    ids = "\n".join(candidate_details or sibling_ids) if sibling_ids else "(없음)"
    if section_headings:
        secs = ", ".join(f'"{h}"' for h in section_headings)
        schema = ('{"tags": ["tag1"], "links": ["id"], '
                  f'"sections": {{{secs}: "내용"}}, '
                  '"evidence": ["원문에서 그대로 복사한 근거 구절"]}')
        section_note = f"- sections: {secs} 각 섹션을 원문 기반으로 채운다. 없으면 빈 문자열.\n"
    else:
        schema = ('{"tags": ["tag1"], "links": ["id"], "body": "마크다운 본문", '
                  '"evidence": ["원문에서 그대로 복사한 근거 구절"]}')
        section_note = "- body: 핵심을 간결한 마크다운으로 작성한다.\n"
    system = (
        f"너는 '{planned['title']}' 주제의 원자 위키 노트 본문을 작성한다. "
        f"범위: {planned.get('scope') or planned['title']}. 이 주제만 다루고 다른 주제는 제외한다. "
        "아래 JSON 객체 하나만 출력한다(설명·코드펜스 금지):\n"
        f"{schema}\n"
        "규칙:\n"
        "- links: 아래 '연결 가능한 ID' 중 관련된 것만. 없으면 빈 배열.\n"
        "- tags: 주제를 나타내는 키워드 2~4개. 노트 id를 tags에 넣지 마라.\n"
        "- 원문에 있는 내용만 쓴다. 부풀리거나 지어내지 않는다.\n"
        "- evidence: 핵심 주장마다 참고 원문에서 그대로 복사한 구절을 넣는다.\n"
        "- 본문에서 다른 노트는 [[id]]로 링크할 수 있다.\n"
        f"{section_note}"
    )
    user = f"연결 가능한 ID: {ids}\n\n=== 참고 원문 ===\n{window_text}\n=== 끝 ==="
    return system, user


def ingest_long_text(
    idx,
    graph,
    raw_text: str,
    *,
    complete_fn: Callable[[str, str], str],
    template: str | None = None,
    template_dir: Path | None = None,
    max_chars: int = 6000,
    max_notes: int = 20,
    source_id: str | None = None,
    require_evidence: bool = False,
) -> dict[str, Any]:
    """긴 문서를 아웃라인→노트별 작성으로 여러 원자 노트로 변환."""
    existing_ids = sorted(idx.all_ids)
    planned, windows = outline_document(raw_text, existing_ids, complete_fn, max_chars, max_notes)
    if not planned:
        return {"status": "error", "error": "아웃라인에서 노트를 계획하지 못함", "results": []}

    planned_ids = [p["id"] for p in planned]
    section_headings: list[str] = []
    if template:
        tmpl = load_template(template_dir, template)
        if tmpl:
            section_headings = template_section_headings(tmpl["body"])

    results: list[dict] = []
    created: list[str] = []
    for p in planned:
        window_text = windows[p["window_idx"]] if p["window_idx"] < len(windows) else raw_text
        sibling_ids = [i for i in planned_ids if i != p["id"]]
        candidates = related_candidates(idx, window_text)
        candidate_ids = [candidate["id"] for candidate in candidates]
        siblings = sibling_ids + [nid for nid in candidate_ids if nid not in sibling_ids]
        details = [f"- {nid} | 같은 원문에서 계획된 형제 노트" for nid in sibling_ids]
        details.extend(_candidate_details(candidates))
        try:
            note = parse_note_json(
                complete_fn(*_write_prompts(window_text, p, siblings, section_headings, details))
            )
            evidence = validate_evidence(window_text, note.get("evidence")) if require_evidence else []
        except Exception as e:
            results.append({"id": p["id"], "status": "error", "error": f"작성 실패: {e}"})
            continue
        raw_sec = note.get("sections") if section_headings else None
        sections = (
            {str(k): str(v) for k, v in raw_sec.items() if v}
            if isinstance(raw_sec, dict)
            else None
        )
        body = str(note.get("body", "") or "")
        if source_id and evidence:
            evidence_md = _evidence_markdown(evidence, source_id)
            if sections:
                last = next(reversed(sections))
                sections[last] = sections[last].rstrip() + "\n\n" + evidence_md
            else:
                body = body.rstrip() + "\n\n" + evidence_md
        note_links, rejected_links = filter_allowed_links(note.get("links"), set(siblings))
        if source_id and source_id not in note_links:
            note_links.append(source_id)
        r = create_note(
            idx, graph,
            id=p["id"],
            title=p["title"],
            body=body,
            tags=[str(t) for t in (note.get("tags") or [])],
            links=note_links,
            source_url=f"wiki://{source_id}" if source_id else "",
            template=template,
            template_dir=template_dir,
            sections=sections,
        )
        results.append({
            "id": p["id"],
            "status": r.get("status"),
            "relative_path": r.get("relative_path"),
            "error": r.get("error"),
            "rejected_links": rejected_links,
        })
        if r.get("status") == "ok":
            created.append(p["id"])

    return {
        "status": "ok" if created else "error",
        "created": created,
        "planned": planned_ids,
        "results": results,
    }


# 원문으로 취급하지 않을 파일명 stem (안내 문서 등)
_SKIP_STEMS = {"readme"}
# 처리 결과 보관 디렉토리 — 재귀 스캔 시 반드시 제외 (무한 재처리 방지)
_RESULT_DIRS = {".processed", ".failed"}


def is_ingestable(path, extensions, raw_dir=None) -> bool:
    """raw 파일이 ingestion 대상인지 판정.

    제외: 숨김 파일/디렉토리(.processed·.failed 포함), README, 미지원 확장자.
    raw_dir를 주면 그 하위의 숨김 디렉토리까지 검사한다(재귀 스캔용).
    """
    p = Path(path)
    if p.name.startswith("."):
        return False
    if p.stem.lower() in _SKIP_STEMS:
        return False
    if p.suffix.lower() not in tuple(e.lower() for e in extensions):
        return False

    # 하위 경로에 숨김 디렉토리(.processed/.failed/.git 등)가 있으면 제외
    if raw_dir:
        try:
            parts = p.resolve().relative_to(Path(raw_dir).resolve()).parts[:-1]
        except ValueError:
            return True
        if any(part.startswith(".") for part in parts):
            return False
    return True


def iter_raw_files(raw_dir: Path | None, extensions) -> list[Path]:
    """raw_dir을 **재귀적으로** 훑어 처리 대상 파일 목록 반환.

    원문을 하위 폴더로 정리해 넣는 사용 방식을 지원한다
    (예: raw/anthropic/api/concepts/*.md).
    숨김 디렉토리(.processed·.failed 등)와 README, 미지원 확장자는 제외한다.
    """
    if not raw_dir or not str(raw_dir):
        return []
    d = Path(raw_dir)
    if not d.is_dir():
        return []
    return sorted(
        p for p in d.rglob("*") if p.is_file() and is_ingestable(p, extensions, raw_dir=d)
    )


def ingest_file(
    idx,
    graph,
    raw_path: Path,
    *,
    complete_fn: Callable[[str, str], str],
    template: str | None = None,
    template_dir: Path | None = None,
    processed_dir: Path | None = None,
    failed_dir: Path | None = None,
    max_chars: int = 0,
    max_notes: int = 20,
    max_extract_chars: int = 200000,
    xlsx_max_rows: int = 200,
    ocr: bool = False,
    ocr_bin=None,
) -> dict[str, Any]:
    """raw 파일 하나를 노트화하고, 성공/실패에 따라 원문을 이동.

    PDF/docx/pptx/xlsx/html은 텍스트를 추출한 뒤 처리한다(오프라인).
    스캔 PDF·이미지는 ocr=True 시 vision-ocr로 OCR 처리한다.
    max_chars > 0 이고 (추출) 원문이 그보다 길면 아웃라인→작성(다중 노트) 경로를 사용한다.
    """
    from wiki_mcp.extract import ExtractionError, extract_text

    raw_path = Path(raw_path)
    result: dict[str, Any]
    # 추출 실패도 아래 이동 로직을 타야 한다(early return 금지) — 그러지 않으면
    # 실패 원문이 raw/에 남아 watch 재시작·ingest_raw 마다 무한 재시도된다.
    raw_text = ""
    try:
        raw_text = extract_text(
            raw_path, max_chars=max_extract_chars, xlsx_max_rows=xlsx_max_rows,
            ocr=ocr, ocr_bin=ocr_bin,
        )
        extract_error = None
    except ExtractionError as e:
        extract_error = f"추출 실패: {e}"
    except Exception as e:
        extract_error = f"읽기 실패: {e}"

    if extract_error:
        result = {"status": "error", "error": extract_error}
    elif not raw_text.strip():
        result = {"status": "error", "error": "빈 파일"}
    else:
        try:
            source = create_source_note(idx, graph, raw_path, raw_text)
            if source.get("status") != "ok":
                raise ValueError(f"원문 Source 노트 저장 실패: {source.get('error')}")
            source_id = str(source["id"])
            if max_chars > 0 and len(raw_text) > max_chars:
                result = ingest_long_text(
                    idx, graph, raw_text,
                    complete_fn=complete_fn,
                    template=template, template_dir=template_dir,
                    max_chars=max_chars, max_notes=max_notes,
                    source_id=source_id, require_evidence=True,
                )
            else:
                result = ingest_text(
                    idx, graph, raw_text,
                    complete_fn=complete_fn,
                    template=template, template_dir=template_dir,
                    source_id=source_id, require_evidence=True,
                )
            result["source_id"] = source_id
            result["source_relative_path"] = source.get("relative_path")
        except Exception as e:
            result = {"status": "error", "error": f"변환 실패: {e}"}

    result["file"] = str(raw_path)

    # 원문 이동 — 성공 시 processed, 실패 시 failed
    dest_dir = processed_dir if result.get("status") == "ok" else failed_dir
    if dest_dir:
        try:
            dest_dir = Path(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            moved = dest_dir / raw_path.name
            shutil.move(str(raw_path), str(moved))
            result["raw_moved_to"] = str(moved)
        except Exception as e:
            result.setdefault("warnings", []).append(f"원문 이동 실패: {e}")
    return result
