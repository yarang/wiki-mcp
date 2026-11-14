"""KB(지식 기반) 모드 — 원본 보존 지식 기반.

WIKI_KB_DIR(또는 CLI --kb)이 정해졌을 때만 동작한다. 원본 폴더는 읽기만 하고
추출 결과·매니페스트·노트는 모두 KB 폴더 안에 둔다. KB 폴더를 지우면 원본에는
아무 흔적도 남지 않는다.

공개 진입점: KnowledgeBase(저장소), add_source/list_sources/remove_source(원본 관리),
scan_sources(결정적 스캔), SourceIndex(추출 결과 검색).
"""

from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.scanner import scan_sources
from wiki_mcp.kb.search import SourceIndex
from wiki_mcp.kb.sources import add_source, list_sources, remove_source
from wiki_mcp.kb.store import KnowledgeBase

__all__ = [
    "KBError",
    "KnowledgeBase",
    "SourceIndex",
    "add_source",
    "list_sources",
    "remove_source",
    "scan_sources",
]
