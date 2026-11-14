"""노트 파일 변경 감지 → 재인덱싱 (mcpo 가 띄운 wiki-mcp 안에서 돈다).

Chroma PersistentClient 는 프로세스마다 HNSW 인덱스를 메모리에 들고 있어서,
다른 프로세스(watch)가 저장소에 쓴 벡터는 이 프로세스의 dense 검색에 보이지 않는다.
그래서 저장소에는 검색하는 이 프로세스만 쓰고, watch 는 Markdown 만 쓴다.
watch·편집기·git 등 누가 노트를 바꾸든 여기서 파일 변경을 보고 build() 한다.

stdio transport 에서는 stdout 이 MCP 프로토콜이므로 로그는 stderr 로만 낸다.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

_CHANGES = {"created", "modified", "deleted", "moved"}


class DebouncedReindex(FileSystemEventHandler):
    """마지막 .md 변경 뒤 `delay` 초 동안 조용하면 build() 를 한 번 부른다 (trailing debounce).

    raw 하나가 노트 여러 개로 바뀌거나 편집기가 여러 번 저장해도 재인덱싱은 한 번이다.
    """

    def __init__(self, indexer, delay: float = 2.0):
        self.indexer = indexer
        self.delay = delay
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def _is_note(self, path: str) -> bool:
        p = Path(path)
        if p.suffix != ".md":
            return False
        try:
            # macOS FSEvents 는 /private/var/... 같은 실제 경로로 알려 준다
            rel = p.resolve().relative_to(Path(self.indexer.wiki_path).resolve())
        except ValueError:
            return False
        # build() 와 같은 규칙 — 숨김 디렉토리(.templates, .git, .obsidian)는 노트가 아님
        return not any(part.startswith(".") for part in rel.parts)

    def on_any_event(self, event: FileSystemEvent) -> None:
        # opened/closed 는 빼야 한다 — Linux inotify 에서는 build() 가 노트를 읽는 것만으로
        # 이벤트가 나서 재인덱싱이 끝없이 이어진다
        if event.is_directory or event.event_type not in _CHANGES:
            return
        paths = [event.src_path, getattr(event, "dest_path", "") or ""]
        if any(self._is_note(p) for p in paths if p):
            self._schedule()

    def _schedule(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self.run)
            self._timer.daemon = True
            self._timer.start()

    def run(self) -> None:
        try:
            stats = self.indexer.build(verbose=False)
            print(f"🔄 노트 변경 감지 → 재인덱싱: {stats}", file=sys.stderr)
        except Exception as e:  # 감시 스레드가 죽으면 이후 변경을 영영 놓친다
            print(f"⚠️  자동 재인덱싱 실패: {e}", file=sys.stderr)


def start(indexer, delay: float = 2.0) -> Observer:
    """위키 폴더 감시를 시작하고 Observer 를 돌려준다 (데몬 스레드 — 프로세스와 함께 끝난다)."""
    observer = Observer()
    observer.daemon = True
    observer.schedule(DebouncedReindex(indexer, delay), str(indexer.wiki_path), recursive=True)
    observer.start()
    return observer
