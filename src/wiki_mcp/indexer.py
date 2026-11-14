"""Wiki 노트 인덱싱 — Hybrid (BM25 + dense embedding)."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from pathlib import Path

import frontmatter
import requests
from rank_bm25 import BM25Okapi

from wiki_mcp.config import EMBEDDING_API_KEY, EMBEDDING_DISABLED, EMBEDDING_PROVIDER

# ─── 토크나이저 ─────────────────────────────────────────────────
_TOKEN_RE = re.compile(r"\[\[[\w\-\.]+\]\]|[\w가-힣]+")
_LINK_RE = re.compile(r"\[\[([^\]|\\]+?)(?:\\?\|[^\]]*)?\]\]")
# 한글 연속 구간 — 조사가 붙은 표기("기준을")에서 어간("기준")이 겹치게 하는 2-gram용
_HANGUL_RUN_RE = re.compile(r"[가-힣]+")


def tokenize(text: str) -> list[str]:
    """한국어 + 영어 + `[[id]]` 동시 처리.

    한글 연속 구간은 글자 2-gram을 원 토큰과 함께 추가한다("기준을" →
    "기준을", "기준", "준을"). 한국어는 조사 때문에 질의와 본문이 한 토큰도
    겹치지 않는 일이 잦아, 어간 검색("기준")이 활용("기준을")을 찾지 못했다.
    한 글자 한글 토큰도 그대로 유지하고 영어·숫자·`[[id]]` 처리는 바꾸지 않는다.
    """
    tokens = _TOKEN_RE.findall(text.lower())
    out: list[str] = []
    for tok in tokens:
        out.append(tok)
        for run in _HANGUL_RUN_RE.findall(tok):
            for i in range(len(run) - 1):
                bigram = run[i : i + 2]
                # 2글자 한글 토큰의 2-gram은 원 토큰과 같으니 중복해 넣지 않는다
                if bigram != tok:
                    out.append(bigram)
    return out


# ─── chromadb 지연 로딩 ─────────────────────────────────────────
# 0.3.0부터 chromadb는 선택 의존성([embed])이다. BM25 전용 모드에서는 설치
# 부담을 줄이기 위해 import 자체를 미룬다. 없으면 stderr에 안내를 한 번만
# 남기고 BM25 전용으로 동작한다.
_CHROMA_TUPLE = (False, None, None)
_CHROMA_RESOLVED = False


def _load_chroma():
    """chromadb를 실제로 쓸 때만 import한다. (available, chromadb, Settings)를 반환."""
    global _CHROMA_TUPLE, _CHROMA_RESOLVED
    if _CHROMA_RESOLVED:
        return _CHROMA_TUPLE
    _CHROMA_RESOLVED = True
    try:
        import chromadb
        from chromadb.config import Settings
    except ImportError:
        print(
            "ℹ️  chromadb 미설치 — BM25 전용 모드로 실행. "
            "dense 검색이 필요하면 `wiki-mcp[embed]`을(를) 설치하세요.",
            file=sys.stderr,
        )
        _CHROMA_TUPLE = (False, None, None)
    else:
        _CHROMA_TUPLE = (True, chromadb, Settings)
    return _CHROMA_TUPLE


def extract_links(content: str) -> list[str]:
    """본문에서 `[[id]]` 모두 추출 (앵커 `#` 제거)."""
    links = _LINK_RE.findall(content)
    return [link.split("#")[0].strip() for link in links if link.strip()]


class WikiIndexer:
    """노트 인덱스 + Hybrid 검색."""

    def __init__(
        self,
        wiki_path: Path,
        data_dir: Path,
        ollama_url: str = "http://localhost:11434",
        embedding_model: str = "bge-m3",
        store: bool = True,
    ):
        """store=False: 벡터 저장소·index_cache.json 을 열지도 쓰지도 않는 BM25 전용 인덱서.

        watch 가 쓴다. Chroma 는 프로세스마다 HNSW 를 메모리에 들고 있어서 다른 프로세스가
        쓴 벡터가 mcpo 쪽 dense 검색에 보이지 않으므로, 저장소에는 mcpo 쪽만 쓴다.
        """
        self.wiki_path = Path(wiki_path)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ollama_url = ollama_url.rstrip("/")
        self.embedding_model = embedding_model
        self.embedding_disabled = EMBEDDING_DISABLED
        self.embedding_provider = EMBEDDING_PROVIDER
        self.embedding_api_key = EMBEDDING_API_KEY

        self.store = store and _load_chroma()[0]
        # build() 는 감시 스레드와 도구 호출 양쪽에서 불린다. writer 도구가 도구 안에서
        # build() 를 다시 부르므로 RLock.
        self._lock = threading.RLock()

        # Chroma (영구) — chromadb가 설치돼 있을 때만. 없으면 BM25 전용으로 강등.
        self.client = None
        self.collection = None
        if self.store:
            chroma_ok, chromadb, settings_cls = _load_chroma()
            self.client = chromadb.PersistentClient(
                path=str(self.data_dir / "chroma"),
                settings=settings_cls(anonymized_telemetry=False),
            )
            self.collection = self._open_collection()

        # 인메모리
        self.notes: dict[str, dict] = {}
        self.all_ids: set[str] = set()
        self.out_links: dict[str, set[str]] = {}
        self.in_links: dict[str, set[str]] = {}
        self.bm25: BM25Okapi | None = None
        self._bm25_doc_ids: list[str] = []

    def _open_collection(self):
        """저장소를 연다. 다른 임베딩 모델로 만든 저장소면 비우고 새로 만든다.

        옛 벡터가 남으면 질의 벡터와 차원·공간이 달라 dense 검색이 매번 실패하고,
        search_hybrid 가 그 실패를 삼켜 BM25 결과만 돌려준다. 모델 이름은 컬렉션
        metadata 에 적는다 (0.2.1 까지는 적지 않았다 — 행이 있으면 모르는 모델로 본다).
        빈 저장소가 되면 build() 의 `note_id not in stored` 가 전부 다시 임베딩한다.
        """
        metadata = {"hnsw:space": "cosine", "embedding_model": self.embedding_model}
        col = self.client.get_or_create_collection(name="wiki", metadata=metadata)
        if self.embedding_disabled:
            return col  # BM25-only 모드 — 저장소를 건드리지 않음
        stored_model = (col.metadata or {}).get("embedding_model")
        if stored_model == self.embedding_model:
            return col
        if stored_model is None and col.count() == 0:
            return col
        print(
            f"임베딩 모델 변경 ({stored_model or '기록 없음'} → {self.embedding_model}): "
            f"저장소를 새로 만든다 ({col.count()}개 다시 임베딩)",
            file=sys.stderr,
        )
        self.client.delete_collection("wiki")
        return self.client.create_collection(name="wiki", metadata=metadata)

    @property
    def dense(self) -> bool:
        """dense 검색·임베딩을 하는가 — 저장소가 있고 임베딩이 켜져 있을 때만."""
        return self.collection is not None and not self.embedding_disabled

    # ─── 임베딩 ──────────────────────────────────────
    # @MX:NOTE: [AUTO] Two backends selected by self.embedding_provider
    #   - "ollama": POST {ollama_url}/api/embeddings, body {"model","prompt"}, resp["embedding"]
    #   - "openai": POST {ollama_url}/v1/embeddings, body {"model","input"}, resp["data"][0]["embedding"]
    #   "openai" covers LM Studio default API + OpenAI/Voyage/Cohere hosted endpoints.
    #   OLLAMA_URL is reused as the base URL for all providers (no env rename).
    def _embed(self, text: str) -> list[float]:
        # @MX:WARN: [AUTO] fail-fast guard — BM25-only 모드에서 _embed 호출 금지
        # @MX:REASON: silent fallback 시 dense 결과 누락이 Hybrid로 위장됨
        if self.embedding_disabled:
            raise RuntimeError("embedding disabled but _embed called")

        provider = self.embedding_provider
        if provider == "ollama":
            r = requests.post(
                f"{self.ollama_url}/api/embeddings",
                json={"model": self.embedding_model, "prompt": text},
                timeout=60,
            )
            r.raise_for_status()
            return r.json()["embedding"]
        elif provider == "openai":
            headers: dict[str, str] = {}
            if self.embedding_api_key:
                headers["Authorization"] = f"Bearer {self.embedding_api_key}"
            r = requests.post(
                f"{self.ollama_url}/v1/embeddings",
                headers=headers,
                json={"model": self.embedding_model, "input": text},
                timeout=60,
            )
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]
        else:
            raise ValueError(f"unknown EMBEDDING_PROVIDER: {provider}")

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    # ─── 인덱싱 ──────────────────────────────────────
    def build(self, force: bool = False, verbose: bool = True) -> dict:
        with self._lock:
            return self._build(force, verbose)

    def _build(self, force: bool, verbose: bool) -> dict:
        cache_file = self.data_dir / "index_cache.json"
        cache: dict[str, str] = {}
        if cache_file.exists() and not force:
            try:
                cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                cache = {}

        # 캐시는 "이 해시의 임베딩이 저장소에 있다"는 뜻 — 임베딩에 성공한 노트만 적는다.
        # 저장소에 없는 id 는 캐시가 맞아도 다시 임베딩 (0.2.0 은 실패한 노트도 적어서
        # 임베딩 서버 없이 처음 인덱싱하면 dense 인덱스가 영영 비어 있었다).
        stored: set[str] = set(self.collection.get(include=[])["ids"]) if self.dense else set()
        new_cache: dict[str, str] = {}
        embed_queue: list[dict] = []
        embedded = 0

        # 노트 스캔
        self.notes.clear()
        self.all_ids.clear()
        self.out_links.clear()
        self.in_links.clear()

        for md in self.wiki_path.rglob("*.md"):
            # 숨김 디렉토리(.templates, .git 등) 내 파일은 노트로 취급하지 않음
            if any(part.startswith(".") for part in md.relative_to(self.wiki_path).parts):
                continue
            try:
                post = frontmatter.load(md)
                note_id = post.get("id")
                if not note_id:
                    continue

                content = post.content
                chash = self._content_hash(content)
                title = post.get("title", "") or ""
                tags = post.get("tags", []) or []
                if isinstance(tags, str):
                    tags = [tags]
                source_url = post.get("source_url", "") or ""

                links = extract_links(content)

                note = {
                    "id": note_id,
                    "title": title,
                    "tags": tags,
                    "source_url": source_url,
                    "path": str(md.relative_to(self.wiki_path)),
                    "content": content,
                    "content_hash": chash,
                    "out_links": links,
                }
                self.notes[note_id] = note
                self.all_ids.add(note_id)

                for target in links:
                    self.out_links.setdefault(note_id, set()).add(target)
                    self.in_links.setdefault(target, set()).add(note_id)

                if cache.get(note_id) != chash or note_id not in stored:
                    embed_queue.append(note)
                else:
                    new_cache[note_id] = chash

            except Exception as e:
                if verbose:
                    print(f"⚠️  {md}: {e}", file=sys.stderr)

        # 변경된 노트만 embed
        if not self.dense:
            # BM25-only 모드 — Ollama/ChromaDB 건드리지 않음
            if verbose:
                print(
                    "🔧 임베딩 비활성화 — BM25-only 모드 (Ollama/LM Studio 불필요)"
                    if self.store
                    else "🔧 벡터 저장소 없음 — BM25 전용 (임베딩은 mcpo 쪽 wiki-mcp 가 맡는다)",
                    file=sys.stderr,
                )
            embed_queue.clear()
            new_cache = cache  # 저장소를 건드리지 않았으니 캐시도 그대로
            deleted: set[str] = set()
        else:
            if embed_queue:
                if verbose:
                    print(f"임베딩 갱신: {len(embed_queue)}개", file=sys.stderr)
                for i, note in enumerate(embed_queue, 1):
                    text = (
                        f"# {note['title']}\n태그: {', '.join(note['tags'])}\n\n{note['content']}"
                    )
                    try:
                        emb = self._embed(text)
                        self.collection.upsert(
                            ids=[note["id"]],
                            embeddings=[emb],
                            documents=[text],
                            metadatas=[
                                {
                                    "title": note["title"],
                                    "tags": ",".join(note["tags"]),
                                    "path": note["path"],
                                }
                            ],
                        )
                        new_cache[note["id"]] = note["content_hash"]
                        embedded += 1
                        if verbose and i % 20 == 0:
                            print(f"  {i}/{len(embed_queue)}", file=sys.stderr)
                    except Exception as e:
                        if verbose:
                            print(f"  ❌ {note['id']}: {e}", file=sys.stderr)

            # 삭제된 노트 cleanup
            deleted = stored - self.all_ids
            if deleted:
                self.collection.delete(ids=list(deleted))
                if verbose:
                    print(f"삭제: {len(deleted)}개", file=sys.stderr)

        # BM25 rebuild
        docs = list(self.notes.values())
        tokenized = [tokenize(f"{n['title']} {' '.join(n['tags'])} {n['content']}") for n in docs]
        self.bm25 = BM25Okapi(tokenized) if tokenized else None
        self._bm25_doc_ids = [n["id"] for n in docs]

        if self.store:  # 저장소 없는 인덱서가 쓰면 mcpo 쪽의 새 캐시를 옛것으로 덮는다
            cache_file.write_text(
                json.dumps(new_cache, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        total_edges = sum(len(v) for v in self.in_links.values())
        stats = {
            "notes": len(self.notes),
            "edges": total_edges,
            "embedded": embedded,
            "deleted": len(deleted),
        }
        if verbose:
            print(
                f"✅ 인덱싱 완료 — {stats['notes']}개 노트, {stats['edges']}개 링크",
                file=sys.stderr,
            )
        return stats

    # ─── 검색 ──────────────────────────────────────
    def search_hybrid(
        self,
        query: str,
        top_k: int = 10,
        tag_filter: str | None = None,
    ) -> list[dict]:
        """Dense + BM25 + RRF fusion."""
        with self._lock:
            return self._search_hybrid(query, top_k, tag_filter)

    def _search_hybrid(self, query: str, top_k: int, tag_filter: str | None) -> list[dict]:
        if not self.bm25:
            return []

        candidates: dict[str, float] = {}

        # Dense (BM25-only 모드에서는 스킵)
        if self.dense:
            try:
                q_emb = self._embed(query)
                dense_res = self.collection.query(
                    query_embeddings=[q_emb],
                    n_results=top_k * 2,
                )
                dense_ids = dense_res["ids"][0]
                for rank, nid in enumerate(dense_ids):
                    candidates[nid] = candidates.get(nid, 0.0) + 1.0 / (60 + rank)
            except Exception as e:
                print(f"⚠️  Dense search failed: {e}", file=sys.stderr)

        # BM25 — 질의 토큰이 하나도 없는 노트는 표를 주지 않는다. 주면 한국어처럼 조사 때문에
        # 토큰이 안 겹치는 질의에서 아무 노트나 순서대로 표를 받아 dense 1위를 밀어낸다.
        # 점수(>0)로 거르면 안 된다 — 노트가 적거나 흔한 단어면 IDF 가 0 이하라 맞는 노트도 0 이하다.
        try:
            tokens = tokenize(query)
            q_tokens = set(tokens)
            scores = self.bm25.get_scores(tokens)
            bm25_ranked = sorted(
                (
                    (i, s)
                    for i, s in enumerate(scores)
                    if not q_tokens.isdisjoint(self.bm25.doc_freqs[i])
                ),
                key=lambda x: -x[1],
            )[: top_k * 2]
            for rank, (doc_idx, _) in enumerate(bm25_ranked):
                nid = self._bm25_doc_ids[doc_idx]
                candidates[nid] = candidates.get(nid, 0.0) + 1.0 / (60 + rank)
        except Exception as e:
            print(f"⚠️  BM25 failed: {e}", file=sys.stderr)

        # RRF 순위 + tag filter
        ranked = sorted(candidates.items(), key=lambda x: -x[1])
        results: list[dict] = []
        for nid, score in ranked:
            if nid not in self.notes:
                continue
            note = self.notes[nid]
            if tag_filter and tag_filter not in note["tags"]:
                continue
            results.append({**note, "score": score})
            if len(results) >= top_k:
                break

        return results
