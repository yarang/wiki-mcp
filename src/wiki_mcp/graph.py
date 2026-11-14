"""Wiki [[id]] link graph 분석."""

from __future__ import annotations

from collections import deque
from pathlib import PureWindowsPath


class WikiGraph:
    def __init__(self, indexer):
        self.idx = indexer

    def neighbors(self, note_id: str, depth: int = 1) -> dict[int, list[str]]:
        """BFS로 N-depth 이웃 (in + out)."""
        if note_id not in self.idx.notes:
            return {}
        visited = {note_id}
        levels: dict[int, list[str]] = {0: [note_id]}
        frontier = deque([(note_id, 0)])
        while frontier:
            nid, d = frontier.popleft()
            if d >= depth:
                continue
            related = self.idx.out_links.get(nid, set()) | self.idx.in_links.get(nid, set())
            for target in related:
                if target in visited or target not in self.idx.notes:
                    continue
                visited.add(target)
                levels.setdefault(d + 1, []).append(target)
                frontier.append((target, d + 1))
        return levels

    def find_broken_links(self) -> list[dict]:
        broken: list[dict] = []
        for nid, targets in self.idx.out_links.items():
            for t in targets:
                if t not in self.idx.all_ids:
                    broken.append({"source": nid, "broken_target": t})
        return broken

    def hub_score(self, note_id: str) -> int:
        return len(self.idx.in_links.get(note_id, set()))

    def domain_of(self, note_id: str) -> str:
        if note_id not in self.idx.notes:
            return ""
        # rel_path의 첫 세그먼트. 저장은 as_posix()로 통일하지만, 구(버전)
        # 색인처럼 '\' 표기가 섞여 들어와도 PureWindowsPath가 두 구분자를
        # 모두 인식해 도메인이 통째로 노출되지 않게 한다.
        parts = PureWindowsPath(str(self.idx.notes[note_id]["path"])).parts
        return parts[0] if parts else ""

    def list_domains(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for nid in self.idx.notes:
            d = self.domain_of(nid)
            counts[d] = counts.get(d, 0) + 1
        return counts

    def top_hubs(self, limit: int = 10) -> list[tuple[str, int]]:
        scored = [
            (nid, len(refs)) for nid, refs in self.idx.in_links.items() if nid in self.idx.notes
        ]
        scored.sort(key=lambda x: -x[1])
        return scored[:limit]
