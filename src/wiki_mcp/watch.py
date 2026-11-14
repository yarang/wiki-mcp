"""파일 시스템 watcher — wiki 변경 시 자동 재인덱싱 + (옵션) raw → wiki 자동 변환.

벡터 저장소(Chroma)와 index_cache.json 에는 쓰지 않는다 (store=False, BM25 전용).
Chroma 는 프로세스마다 HNSW 를 메모리에 두므로 여기서 쓴 벡터는 mcpo 쪽 dense 검색에
보이지 않았다. 임베딩은 mcpo 가 띄운 wiki-mcp 가 노트 파일 변경을 보고 스스로 한다
(autoindex). 여기 인덱스는 raw 변환 때 중복 id 확인과 링크 후보(BM25)에만 쓴다.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from wiki_mcp import config
from wiki_mcp.config import DATA_DIR, EMBEDDING_MODEL, OLLAMA_URL, WIKI_PATH
from wiki_mcp.graph import WikiGraph
from wiki_mcp.indexer import WikiIndexer
from wiki_mcp.ingest import (
    ingest_file,
    is_ingestable,
    iter_raw_files,
    openai_chat_complete,
)


class ReindexHandler(FileSystemEventHandler):
    def __init__(self, indexer: WikiIndexer, debounce_sec: float = 5.0):
        self.idx = indexer
        self.debounce = debounce_sec
        self._last_run = 0.0
        self._pending = False

    def _trigger(self, src: str):
        now = time.time()
        if now - self._last_run < self.debounce:
            self._pending = True
            return
        self._last_run = now
        self._pending = False
        print(f"🔄 변경 감지: {src}", file=sys.stderr)
        try:
            self.idx.build()
        except Exception as e:
            print(f"❌ 재인덱싱 실패: {e}", file=sys.stderr)

    def on_modified(self, event):
        if event.is_directory:
            return
        if not str(event.src_path).endswith(".md"):
            return
        self._trigger(str(event.src_path))

    def on_created(self, event):
        self.on_modified(event)

    def on_deleted(self, event):
        self.on_modified(event)


class RawIngestHandler(FileSystemEventHandler):
    """raw 디렉토리에 새 원문이 들어오면 LLM으로 노트화."""

    def __init__(self, idx, graph, complete_fn, *, settle_sec: float = 2.0):
        self.idx = idx
        self.graph = graph
        self.complete_fn = complete_fn
        self.settle = settle_sec
        self._processed_dir = config.RAW_DIR / ".processed"
        self._failed_dir = config.RAW_DIR / ".failed"

    def _rel(self, p: Path) -> Path:
        """raw 루트 기준 상대경로 (하위 폴더 구조 보존용)."""
        try:
            return p.resolve().relative_to(Path(config.RAW_DIR).resolve())
        except ValueError:
            return Path(p.name)

    def _handle(self, path_str: str):
        p = Path(path_str)
        if not is_ingestable(p, config.INGEST_EXTENSIONS, raw_dir=config.RAW_DIR):
            return
        if not p.exists():
            return
        # 파일 쓰기가 끝날 때까지 잠깐 대기 (크기 안정화)
        time.sleep(self.settle)
        rel = self._rel(p)
        print(f"📥 raw 변환 시작: {rel}", file=sys.stderr)
        # 원문 이동 시 하위 폴더 구조를 보존한다 (raw/a/b.md → .processed/a/b.md)
        sub = rel.parent
        result = ingest_file(
            self.idx,
            self.graph,
            p,
            complete_fn=self.complete_fn,
            template=(config.INGEST_TEMPLATE or None),
            template_dir=config.TEMPLATE_DIR,
            processed_dir=self._processed_dir / sub,
            failed_dir=self._failed_dir / sub,
            max_chars=config.INGEST_MAX_CHARS,
            max_notes=config.INGEST_MAX_NOTES,
            max_extract_chars=config.INGEST_MAX_EXTRACT_CHARS,
            xlsx_max_rows=config.INGEST_XLSX_MAX_ROWS,
            ocr=config.INGEST_OCR,
            ocr_bin=config.INGEST_OCR_BIN,
        )
        if result.get("status") == "ok":
            if "created" in result:  # 긴 문서 → 다중 노트
                print(
                    f"✅ {len(result['created'])}개 노트 생성: {', '.join(result['created'])}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"✅ 노트 생성: {result.get('id')} → {result.get('relative_path')}",
                    file=sys.stderr,
                )
        else:
            print(f"❌ 변환 실패({rel}): {result.get('error')}", file=sys.stderr)

    def on_created(self, event):
        if event.is_directory:
            return
        self._handle(str(event.src_path))

    def on_modified(self, event):
        if event.is_directory:
            return
        self._handle(str(event.src_path))


def _make_complete_fn():
    """config 기반 LLM 호출 클로저 생성 (system, user) -> content."""
    return lambda system, user: openai_chat_complete(
        config.INGEST_URL,
        config.INGEST_MODEL,
        config.INGEST_API_KEY,
        system,
        user,
        timeout=config.INGEST_TIMEOUT,
    )


def _ingestion_ready() -> tuple[bool, str]:
    """ingestion 활성 조건 확인 → (가능여부, 사유)."""
    if not config.INGEST_ENABLED:
        return False, "WIKI_INGEST_ENABLED 미설정"
    if not config.INGEST_MODEL:
        return False, "WIKI_INGEST_MODEL 미설정"
    if not str(config.RAW_DIR):
        return False, "RAW_DIR 미설정"
    return True, "ok"


def main():
    idx = WikiIndexer(
        wiki_path=WIKI_PATH,
        data_dir=DATA_DIR,
        ollama_url=OLLAMA_URL,
        embedding_model=EMBEDDING_MODEL,
        store=False,
    )
    idx.build()
    graph = WikiGraph(idx)

    handler = ReindexHandler(idx, debounce_sec=5.0)
    observer = Observer()
    observer.schedule(handler, str(WIKI_PATH), recursive=True)
    print(f"👀 Watching {WIKI_PATH} (debounce 5s)", file=sys.stderr)

    # raw → wiki ingestion (옵션)
    raw_handler = None
    ready, reason = _ingestion_ready()
    if ready:
        config.RAW_DIR.mkdir(parents=True, exist_ok=True)
        complete_fn = _make_complete_fn()
        raw_handler = RawIngestHandler(idx, graph, complete_fn)
        # 재귀 감시 — 원문을 하위 폴더로 정리해 넣는 사용 방식을 지원한다.
        # .processed/.failed 로의 이동 이벤트는 is_ingestable(raw_dir=...)에서 걸러진다.
        observer.schedule(raw_handler, str(config.RAW_DIR), recursive=True)
        print(
            f"📥 raw ingestion 활성: {config.RAW_DIR} "
            f"(모델 {config.INGEST_MODEL} @ {config.INGEST_URL})",
            file=sys.stderr,
        )
        # 시작 시 이미 쌓인 원문 처리
        pending = iter_raw_files(config.RAW_DIR, config.INGEST_EXTENSIONS)
        if pending:
            print(f"📥 대기 중 원문 {len(pending)}개 처리", file=sys.stderr)
            for p in pending:
                raw_handler._handle(str(p))
    else:
        print(f"ℹ️  raw ingestion 비활성 ({reason}) — 재인덱싱만 수행", file=sys.stderr)

    observer.start()
    try:
        while True:
            time.sleep(10)
            # debounce 끝나면 보류 작업 실행
            if handler._pending and (time.time() - handler._last_run >= handler.debounce):
                handler._last_run = time.time()
                handler._pending = False
                print("🔄 보류된 재인덱싱 실행", file=sys.stderr)
                try:
                    idx.build()
                except Exception as e:
                    print(f"❌ 실패: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
