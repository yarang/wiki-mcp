"""FastMCP server — LLM Wiki를 MCP tool로 노출."""
from __future__ import annotations

import re
import sys
from pathlib import Path

from wiki_mcp.config import (
    DATA_DIR,
    DEFAULT_TEMPLATE,
    EMBEDDING_MODEL,
    OLLAMA_URL,
    TEMPLATE_DIR,
    WIKI_PATH,
)
from wiki_mcp.graph import WikiGraph
from wiki_mcp.indexer import WikiIndexer
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("LLM Wiki")

idx = WikiIndexer(
    wiki_path=WIKI_PATH,
    data_dir=DATA_DIR,
    ollama_url=OLLAMA_URL,
    embedding_model=EMBEDDING_MODEL,
)
print(f"📚 Wiki path: {WIKI_PATH}", file=sys.stderr)
print(f"💾 Data dir : {DATA_DIR}", file=sys.stderr)
print(f"🦙 Ollama   : {OLLAMA_URL} ({EMBEDDING_MODEL})", file=sys.stderr)
mode = (
    "BM25-only (임베딩 비활성화)"
    if idx.embedding_disabled
    else f"Hybrid (BM25 + {EMBEDDING_MODEL} via {idx.embedding_provider})"
)
print(f"🔧 검색 모드: {mode}", file=sys.stderr)
idx.build()
graph = WikiGraph(idx)


# ─── Tools ─────────────────────────────────────────────────────
@mcp.tool()
def search_wiki(query: str, top_k: int = 5, tag_filter: str = "") -> str:
    """LLM Wiki에서 hybrid 검색 (BM25 + dense embedding RRF fusion).

    한국어 + 영어 query 모두 지원. `[[id]]` 형식 정확 매칭에 강함.

    Args:
        query: 검색어
        top_k: 반환할 노트 수 (1-20, 기본 5)
        tag_filter: 특정 태그로 필터 (예: "rag", "mcp", "safety", "claude-code")

    Returns:
        관련 노트 목록 — id, 제목, 도메인, in-degree, 태그, snippet
    """
    top_k = max(1, min(int(top_k), 20))
    tag = tag_filter.strip() or None
    results = idx.search_hybrid(query, top_k=top_k, tag_filter=tag)

    if not results:
        return f"검색 결과 없음 (query: \"{query}\", tag: {tag})"

    lines = [f"# 검색 결과 ({len(results)}개) — query: \"{query}\""]
    if tag:
        lines.append(f"_태그 필터: {tag}_")
    for r in results:
        in_deg = graph.hub_score(r["id"])
        domain = graph.domain_of(r["id"])
        snippet = r["content"][:600].replace("\n\n\n", "\n\n")
        lines.append(
            f"\n## [[{r['id']}]] — {r['title']}\n"
            f"- **도메인**: `{domain}` | **In-degree**: {in_deg} "
            f"| **점수**: {r['score']:.4f}\n"
            f"- **태그**: {', '.join(r['tags'][:6])}\n"
            f"- **경로**: `wiki/{r['path']}`\n\n"
            f"{snippet}...\n"
        )
    return "\n".join(lines)


@mcp.tool()
def read_note(note_id: str) -> str:
    """특정 wiki 노트의 전체 내용 읽기.

    Args:
        note_id: 노트 ID (예: "mcp-server-development", "prompt-xml-tags")

    Returns:
        프론트매터 메타 + 본문 + in/out link 목록
    """
    note_id = note_id.strip().strip("[]")
    if note_id not in idx.notes:
        candidates = [
            nid for nid in idx.all_ids
            if note_id.lower() in nid.lower()
        ][:5]
        msg = f"❌ 노트 [[{note_id}]] 없음."
        if candidates:
            msg += "\n\n비슷한 후보:\n" + "\n".join(
                f"- [[{c}]]" for c in candidates
            )
        return msg

    n = idx.notes[note_id]
    out_links = sorted(idx.out_links.get(note_id, []))
    in_links = sorted(idx.in_links.get(note_id, []))

    out_list = "\n".join(f"- [[{o}]]" for o in out_links[:30]) or "_없음_"
    in_list = "\n".join(f"- [[{i}]]" for i in in_links[:30]) or "_없음_"

    return (
        f"# {n['title']}\n\n"
        f"- **ID**: `{n['id']}`\n"
        f"- **경로**: `wiki/{n['path']}`\n"
        f"- **태그**: {', '.join(n['tags'])}\n"
        f"- **출처**: {n['source_url']}\n"
        f"- **In-degree**: {len(in_links)} | **Out-degree**: {len(out_links)}\n\n"
        f"---\n\n"
        f"{n['content']}\n\n"
        f"---\n\n"
        f"## 이 노트가 가리키는 ({len(out_links)})\n{out_list}\n\n"
        f"## 이 노트를 가리키는 ({len(in_links)})\n{in_list}\n"
    )


@mcp.tool()
def find_related(note_id: str, depth: int = 1) -> str:
    """Link graph 기반 관련 노트 찾기 (BFS N-depth).

    단순 의미 검색과 다르게 wiki의 `[[id]]` 양방향 link 그래프로 연결된 노트를 찾음.
    Atomic notes 간의 명시적 관계를 활용.

    Args:
        note_id: 시작 노트 ID
        depth: 탐색 깊이 (1-3, 기본 1)

    Returns:
        depth별 관련 노트 + in-degree
    """
    note_id = note_id.strip().strip("[]")
    depth = max(1, min(int(depth), 3))
    levels = graph.neighbors(note_id, depth=depth)
    if not levels:
        return f"노트 [[{note_id}]] 없음"

    lines = [f"# [[{note_id}]] 관련 노트 (depth={depth})"]
    for d in sorted(levels.keys()):
        if d == 0:
            continue
        ids = levels[d]
        lines.append(f"\n## Depth {d} ({len(ids)}개)")
        for nid in ids[:40]:
            if nid in idx.notes:
                t = idx.notes[nid]["title"]
                hub = graph.hub_score(nid)
                lines.append(f"- [[{nid}]] — {t} (in-degree {hub})")
    return "\n".join(lines)


@mcp.tool()
def list_domains() -> str:
    """Wiki의 모든 도메인 (top-level 디렉토리) + 노트 수."""
    domains = graph.list_domains()
    total = sum(domains.values())
    lines = [f"# Wiki 도메인 ({total}개 노트, {len(domains)}개 도메인)\n"]
    for d, n in sorted(domains.items(), key=lambda x: -x[1]):
        lines.append(f"- **`{d}`** — {n}개")
    return "\n".join(lines)


@mcp.tool()
def find_broken_links() -> str:
    """존재하지 않는 [[id]]를 참조하는 노트 찾기 (wiki 유지보수).

    Returns:
        broken link 목록 (source → broken target). 없으면 ✅
    """
    broken = graph.find_broken_links()
    if not broken:
        return "✅ Broken link 없음"

    # 동일 source 묶기
    by_source: dict[str, list[str]] = {}
    for b in broken:
        by_source.setdefault(b["source"], []).append(b["broken_target"])

    lines = [f"# Broken Links ({len(broken)}개, {len(by_source)}개 소스)\n"]
    for src, targets in sorted(by_source.items()):
        lines.append(f"\n### [[{src}]]")
        for t in targets:
            lines.append(f"- → [[{t}]] _(존재 X)_")
    return "\n".join(lines)


@mcp.tool()
def suggest_new_note(topic: str, top_k: int = 8) -> str:
    """새 노트 작성 시 도움 — 관련 기존 노트, 권장 cross-link, 권장 위치, 권장 ID.

    Wiki 일관성 유지의 핵심 도구.

    Args:
        topic: 새 노트 주제 (예: "GraphRAG", "Sparse attention", "Llama Guard 4")
        top_k: 관련 기존 노트 수 (기본 8)

    Returns:
        권장 ID, 권장 경로, 관련 노트, 추가할 cross-link, frontmatter 템플릿
    """
    top_k = max(3, min(int(top_k), 15))
    related = idx.search_hybrid(topic, top_k=top_k)
    if not related:
        return "관련 노트 없음 — 완전히 새로운 도메인일 수 있음. `list_domains()`로 기존 구조 확인 권장."

    # 권장 ID (kebab-case)
    suggested_id = re.sub(r"[^\w가-힣]+", "-", topic.lower()).strip("-")[:60]

    # 권장 경로 — 가장 관련된 상위 3개 노트의 다수결 도메인
    domains = [graph.domain_of(r["id"]) for r in related[:3]]
    top_domain = max(set(domains), key=domains.count) if domains else "concepts"

    # Cross-link 후보 (상위 5개)
    cross_links = [r["id"] for r in related[:5]]

    lines = [
        f"# 새 노트 제안 — \"{topic}\"\n",
        f"## 권장 ID\n`{suggested_id}`\n",
        f"## 권장 경로\n`wiki/{top_domain}/concepts/{suggested_id}.md`\n",
        f"_(또는 `wiki/{top_domain}/guides/{suggested_id}.md`)_\n",
        f"## 관련 기존 노트 ({len(related)}개)",
    ]
    for r in related:
        lines.append(
            f"- [[{r['id']}]] — {r['title']} "
            f"(점수 {r['score']:.3f}, 도메인 `{graph.domain_of(r['id'])}`)"
        )

    lines.extend([
        f"\n## 새 노트에 추가할 cross-link (권장 5개)",
        *(f"- [[{nid}]]" for nid in cross_links),
        f"\n## Frontmatter 템플릿\n",
        "```yaml",
        "---",
        f"id: {suggested_id}",
        f'title: "{topic}"',
        'source_url: ""',
        "tags: []",
        'created_at: "2026-06-22"',
        'last_updated: "2026-06-22"',
        "---",
        "```",
    ])
    return "\n".join(lines)


@mcp.tool()
def get_wiki_stats() -> str:
    """Wiki 전체 통계 — 노트 수, link edges, 도메인, 상위 hub 노트."""
    total = len(idx.notes)
    edges = sum(len(v) for v in idx.in_links.values())
    domains = graph.list_domains()
    hubs = graph.top_hubs(limit=15)

    lines = [
        f"# Wiki 통계\n",
        f"- **노트**: {total}개",
        f"- **Link edges**: {edges}개",
        f"- **도메인**: {len(domains)}개",
        f"- **평균 in-degree**: {edges/total:.2f}",
        f"\n## 도메인별 노트 수",
    ]
    for d, n in sorted(domains.items(), key=lambda x: -x[1])[:15]:
        lines.append(f"- `{d}` — {n}")

    lines.append(f"\n## 상위 Hub 노트 (in-degree)")
    for i, (nid, count) in enumerate(hubs, 1):
        title = idx.notes[nid]["title"] if nid in idx.notes else "(missing)"
        lines.append(f"{i}. [[{nid}]] — {title} ({count}개 참조)")

    return "\n".join(lines)


@mcp.tool()
def verify_citations(text: str) -> str:
    """주어진 텍스트의 모든 `[[id]]` 인용을 wiki와 대조 — hallucination 검출.

    LLM 응답 직후 호출하면 존재하지 않는 ID를 인용했는지 확인 가능.

    Args:
        text: 검증할 텍스트 (LLM 응답)

    Returns:
        총 인용 수, 유효, 무효 ID 목록
    """
    cited = re.findall(r"\[\[([^\]|\\]+?)(?:\\?\|[^\]]*)?\]\]", text)
    cited = [c.split("#")[0].strip() for c in cited]
    cited = [c for c in cited if c and c != "id"]

    if not cited:
        return "인용 없음"

    valid = sorted({c for c in cited if c in idx.all_ids})
    invalid = sorted({c for c in cited if c not in idx.all_ids})

    lines = [
        "## 인용 검증",
        f"- 총: {len(cited)}",
        f"- 유효 unique: {len(valid)}",
        f"- ❌ 무효 unique: {len(invalid)}",
    ]
    if invalid:
        lines.append("\n### Hallucinated IDs")
        for c in invalid:
            # fuzzy 제안
            suggestions = [
                nid for nid in idx.all_ids
                if c.lower() in nid.lower() or nid.lower() in c.lower()
            ][:3]
            extra = f" → 비슷: {', '.join(f'[[{s}]]' for s in suggestions)}" if suggestions else ""
            lines.append(f"- `{c}`{extra}")
    return "\n".join(lines)


@mcp.tool()
def reindex(force: bool = False) -> str:
    """Wiki 변경 후 인덱스 재빌드 (변경된 노트만 re-embed).

    Args:
        force: True면 전체 강제 재임베딩
    """
    stats = idx.build(force=force, verbose=True)
    # graph 자동으로 indexer 참조하므로 별도 rebuild 불필요
    return (
        f"✅ 재인덱싱 완료\n"
        f"- 노트: {stats['notes']}\n"
        f"- 링크: {stats['edges']}\n"
        f"- 재임베딩: {stats['embedded']}\n"
        f"- 삭제: {stats['deleted']}"
    )


# ─── Write Tools (안전장치 포함) ────────────────────────────────
@mcp.tool()
def create_note(
    id: str,
    title: str,
    body: str,
    tags: list[str],
    domain: str = "",
    links: list[str] = None,
    source_url: str = "",
    force: bool = False,
    dry_run: bool = False,
    template: str = "",
) -> str:
    """새 wiki 노트 생성 — ID 충돌 시 force=True 아니면 거부. dry_run=True 시 미리보기.

    안전장치:
        - ID 검증 (kebab-case, 한글 거부, 1-80자)
        - 충돌 시 force=True 아니면 거부 (기존 노트 보호)
        - 유사 ID 경고 (오타 감지)
        - dry_run=True 시 파일 미작성, 미리보기 반환
        - force 덮어쓰기 시 자동 .bak 백업

    Args:
        id: 노트 ID (kebab-case, 예: "prompt-chaining")
        title: 노트 제목 (한국어 가능)
        body: 노트 본문 (Markdown). 템플릿 사용 시 템플릿의 {{body}} 자리에 삽입됨.
        tags: 태그 목록 (예: ["rag", "mcp"]). 비우면 템플릿 기본 tags 사용.
        domain: 도메인 (지정 시 <domain>/concepts/<id>.md). 빈 값 시 자동 추정.
        links: cross-link할 기존 노트 ID 목록. '## 관련 노트'(또는 템플릿 {{links}})에 추가.
        source_url: 출처 URL (빈 값 가능)
        force: True 시 동일 ID 덮어쓰기 (기본 False — 충돌 시 거부)
        dry_run: True 시 미리보기만 반환, 파일 미작성 (기본 False)
        template: 적용할 템플릿 이름 (list_templates 참고). 빈 값 시 기본 템플릿
                  (WIKI_DEFAULT_TEMPLATE) 사용, 그것도 없으면 템플릿 미사용.

    Returns:
        JSON — status (ok/error/preview), path, warnings, backup
    """
    from wiki_mcp.writer import create_note as _create
    import json
    effective_template = (template or DEFAULT_TEMPLATE) or None
    result = _create(
        idx, graph, id, title, body, tags,
        domain=domain or None,
        links=links or [],
        source_url=source_url,
        force=force,
        dry_run=dry_run,
        template=effective_template,
        template_dir=TEMPLATE_DIR,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def ingest_raw(file: str = "", limit: int = 10) -> str:
    """raw 디렉토리의 원문을 LLM으로 위키 노트로 변환 (수동 트리거).

    watch 데몬의 자동 변환과 동일한 경로를 사용하되, 필요할 때 즉시 실행한다.
    Open WebUI 등 OpenAI 호환 엔드포인트 설정(WIKI_INGEST_*)이 필요하다.

    Args:
        file: 특정 파일명만 처리 (raw 디렉토리 기준). 빈 값 시 디렉토리 전체.
        limit: 전체 처리 시 최대 개수 (기본 10).

    Returns:
        JSON — 처리 결과 목록 (파일별 status/id/error).
    """
    import json
    from wiki_mcp import config
    from wiki_mcp.ingest import ingest_file, iter_raw_files, openai_chat_complete

    if not config.INGEST_MODEL:
        return json.dumps({
            "status": "error",
            "error": "WIKI_INGEST_MODEL 미설정 — Open WebUI 모델명을 지정하세요 (WIKI_INGEST_URL/API_KEY 포함).",
        }, ensure_ascii=False, indent=2)

    if not config.RAW_DIR or not Path(config.RAW_DIR).is_dir():
        return json.dumps({
            "status": "error",
            "error": f"raw 디렉토리 없음: {config.RAW_DIR}",
        }, ensure_ascii=False, indent=2)

    if file:
        targets = [Path(config.RAW_DIR) / file]
        if not targets[0].exists():
            return json.dumps({
                "status": "error", "error": f"파일 없음: {targets[0]}",
            }, ensure_ascii=False, indent=2)
    else:
        targets = iter_raw_files(config.RAW_DIR, config.INGEST_EXTENSIONS)[: max(1, int(limit))]

    if not targets:
        return json.dumps({
            "status": "ok", "message": "처리할 원문 없음", "results": [],
        }, ensure_ascii=False, indent=2)

    def complete_fn(system, user):
        return openai_chat_complete(
            config.INGEST_URL, config.INGEST_MODEL, config.INGEST_API_KEY,
            system, user, timeout=config.INGEST_TIMEOUT,
        )

    def _sub(p: Path) -> Path:
        """raw 루트 기준 하위 폴더 (원문 이동 시 구조 보존)."""
        try:
            return p.resolve().relative_to(Path(config.RAW_DIR).resolve()).parent
        except ValueError:
            return Path()

    results = []
    for p in targets:
        sub = _sub(p)
        r = ingest_file(
            idx, graph, p,
            complete_fn=complete_fn,
            template=(config.INGEST_TEMPLATE or None),
            template_dir=config.TEMPLATE_DIR,
            processed_dir=Path(config.RAW_DIR) / ".processed" / sub,
            failed_dir=Path(config.RAW_DIR) / ".failed" / sub,
            max_chars=config.INGEST_MAX_CHARS,
            max_notes=config.INGEST_MAX_NOTES,
            max_extract_chars=config.INGEST_MAX_EXTRACT_CHARS,
            xlsx_max_rows=config.INGEST_XLSX_MAX_ROWS,
            ocr=config.INGEST_OCR,
            ocr_bin=config.INGEST_OCR_BIN,
        )
        results.append({
            k: r.get(k)
            for k in ("file", "status", "id", "created", "relative_path", "error")
            if r.get(k) is not None
        })

    return json.dumps({"status": "ok", "results": results}, ensure_ascii=False, indent=2)


@mcp.tool()
def list_templates() -> str:
    """사용 가능한 노트 템플릿 목록 (wiki/.templates/*.md).

    create_note(template="<이름>") 로 적용할 수 있는 양식 목록을 반환한다.
    """
    from wiki_mcp.writer import list_templates as _list
    names = _list(TEMPLATE_DIR)
    if not names:
        return (
            f"템플릿 없음 (디렉토리: {TEMPLATE_DIR}).\n"
            f"`<wiki>/.templates/<이름>.md` 파일을 추가하면 create_note(template=...)로 사용할 수 있습니다."
        )
    lines = [f"사용 가능한 템플릿 ({len(names)}개) — 디렉토리: {TEMPLATE_DIR}", ""]
    lines.extend(f"- {n}" for n in names)
    return "\n".join(lines)


@mcp.tool()
def update_note(
    id: str,
    body: str = None,
    title: str = None,
    tags: list[str] = None,
    append: bool = False,
    dry_run: bool = False,
) -> str:
    """기존 wiki 노트 수정 — 부분 수정 가능, 자동 .bak 백업, last_updated 자동 갱신.

    안전장치:
        - 수정 전 자동 백업 (<name>.bak.<timestamp>)
        - body/title/tags 각각 독립적 부분 수정 (None 시 해당 필드 유지)
        - last_updated 자동 갱신 (오늘 날짜)
        - dry_run=True 시 변경사항 미리보기만 반환

    Args:
        id: 수정할 노트 ID
        body: 새 본문. None 시 본문 유지. append=True면 기존 본문 끝에 추가.
        title: 새 제목. None 시 제목 유지.
        tags: 새 태그 목록. None 시 태그 유지.
        append: True 시 body를 기존 본문에 추가 (교체 아님). 기본 False.
        dry_run: True 시 미리보기만 반환, 파일 미수정. 기본 False.

    Returns:
        JSON — status (ok/error/preview), path, backup, changes
    """
    from wiki_mcp.writer import update_note as _update
    import json
    result = _update(
        idx, id,
        body=body, title=title, tags=tags,
        append=append, dry_run=dry_run,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def add_link(
    id: str,
    target_id: str,
    display: str = "",
    dry_run: bool = False,
) -> str:
    """소스 노트에 [[target_id]] 링크 추가 — '## 관련 노트' 섹션에 단방향 링크.

    안전장치:
        - 대상 노트(target_id)가 없으면 거부 (broken link 사전 방지)
        - 이미 링크 존재 시 변경 없음 (idempotent)
        - 수정 전 자동 백업
        - 양방향 링크 원할 시 add_link를 양쪽으로 두 번 호출 필요

    Args:
        id: 소스 노트 ID (링크를 추가할 노트)
        target_id: 대상 노트 ID (링크 대상 — 존재해야 함)
        display: 링크 표시 텍스트. 빈 값 시 대상 노트의 title 사용.
        dry_run: True 시 미리보기만 반환. 기본 False.

    Returns:
        JSON — status (ok/error/preview), link_added, section_created, backup
    """
    from wiki_mcp.writer import add_link as _add_link
    import json
    result = _add_link(
        idx, id, target_id,
        display=display or None,
        dry_run=dry_run,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def delete_note(id: str, confirm: bool = False, dry_run: bool = False) -> str:
    """wiki 노트 삭제 — confirm=True 필수 (파괴적 연산). 자동 백업 후 삭제.

    안전장치:
        - confirm=True 반드시 필요 (실수로 인한 삭제 방지)
        - 삭제 전 자동 백업 (<name>.bak.<timestamp>)
        - 삭제 후 inbound link가 broken 될 노트 목록 반환
        - dry_run=True 시 삭제 미리보기 (inbound 영향 포함)

    Args:
        id: 삭제할 노트 ID
        confirm: True MUST — 확인 없는 삭제 거부. 기본 False.
        dry_run: True 시 미리보기만 반환, 파일 미삭제. 기본 False.

    Returns:
        JSON — status (ok/error/preview), backup, inbound_links_broken, message
    """
    from wiki_mcp.writer import delete_note as _delete
    import json
    result = _delete(idx, id, confirm=confirm, dry_run=dry_run)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _run() -> None:
    """transport 환경 변수에 따라 stdio 또는 Streamable HTTP로 실행.

    기본 (WIKI_MCP_TRANSPORT 미설정) → stdio (기존 동작 100% 보존).
    """
    # 저장소에 쓰는 건 이 프로세스뿐이다 — watch 등 밖에서 바뀐 노트도 여기서 임베딩해야
    # dense 검색에 바로 잡힌다 (Chroma 는 프로세스마다 HNSW 를 메모리에 둔다).
    from wiki_mcp import autoindex

    try:
        autoindex.start(idx)
        print(f"👀 노트 변경 감지: {WIKI_PATH}", file=sys.stderr)
    except Exception as e:  # 위키 폴더가 없는 등 — 검색은 띄우고, 갱신은 reindex 도구로
        print(f"⚠️  노트 변경 감지를 못 켰다 ({e}) — 바뀐 노트는 reindex 도구로 반영", file=sys.stderr)

    # http 전용 config는 이 브랜치 안에서만 lazy import (stdio 경로 영향 최소화)
    from wiki_mcp.config import (
        WIKI_MCP_API_KEY,
        WIKI_MCP_HOST,
        WIKI_MCP_PATH,
        WIKI_MCP_PORT,
        WIKI_MCP_TRANSPORT,
    )

    if WIKI_MCP_TRANSPORT == "http":
        import uvicorn

        from wiki_mcp.auth import install_auth

        # NOTE: FastMCP 1.28 의 streamable_http_app()은 path 인자를 받지 않음.
        # path는 settings.streamable_http_path에서 읽으므로 여기서 세팅.
        mcp.settings.streamable_http_path = WIKI_MCP_PATH
        app = mcp.streamable_http_app()
        install_auth(app, WIKI_MCP_API_KEY)

        print(
            f"🌐 Streamable HTTP endpoint: "
            f"http://{WIKI_MCP_HOST}:{WIKI_MCP_PORT}{WIKI_MCP_PATH}",
            file=sys.stderr,
        )
        if not WIKI_MCP_API_KEY:
            print(
                "⚠️  인증 없음 — 127.0.0.1 에서만 안전. 원격은 reverse proxy + TLS 권장.",
                file=sys.stderr,
            )

        uvicorn.run(app, host=WIKI_MCP_HOST, port=WIKI_MCP_PORT)
    else:
        # stdio (기존 기본 동작)
        mcp.run()


def main() -> None:
    """CLI entrypoint — wired via `wiki-mcp` console script and `python -m wiki_mcp`."""
    _run()


if __name__ == "__main__":
    main()
