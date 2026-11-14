"""writer.py characterization 테스트 — ID 검증·frontmatter 포맷·경로·CRUD 계약 고정."""
from __future__ import annotations

import pytest

from wiki_mcp.writer import (
    _build_note_path,
    _fill_sections,
    _format_fm_value,
    add_link,
    build_frontmatter,
    create_note,
    delete_note,
    list_templates,
    load_template,
    update_note,
    validate_note_id,
)


# ─── validate_note_id ────────────────────────────────────────────────
class TestValidateNoteId:
    def test_valid_kebab(self):
        assert validate_note_id("prompt-engineering") == "prompt-engineering"

    def test_valid_with_digits(self):
        assert validate_note_id("gpt-4o") == "gpt-4o"

    def test_strips_whitespace(self):
        assert validate_note_id("  rag  ") == "rag"

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="비어있음"):
            validate_note_id("")

    def test_hangul_rejected(self):
        with pytest.raises(ValueError, match="한글"):
            validate_note_id("프롬프트-엔지니어링")

    def test_uppercase_rejected(self):
        # 대문자는 자동 소문자화하지 않고 거부한다
        with pytest.raises(ValueError, match="형식 오류"):
            validate_note_id("RAG")

    def test_leading_hyphen_rejected(self):
        with pytest.raises(ValueError, match="형식 오류"):
            validate_note_id("-rag")

    def test_over_80_chars_rejected(self):
        with pytest.raises(ValueError, match="길이 초과"):
            validate_note_id("a" * 81)


# ─── _format_fm_value ────────────────────────────────────────────────
class TestFormatFmValue:
    def test_bool(self):
        assert _format_fm_value(True) == "true"
        assert _format_fm_value(False) == "false"

    def test_int(self):
        assert _format_fm_value(3) == "3"

    def test_list(self):
        assert _format_fm_value(["a", "b"]) == "[a, b]"

    def test_str_quoted(self):
        assert _format_fm_value("draft") == '"draft"'

    def test_str_escapes_quote(self):
        assert _format_fm_value('a"b') == '"a\\"b"'


# ─── build_frontmatter ───────────────────────────────────────────────
class TestBuildFrontmatter:
    def test_standard_fields_order_and_format(self):
        fm = build_frontmatter(
            id="rag",
            title="검색 증강 생성",
            source_url="http://x",
            tags=["llm", "retrieval"],
            created_at="2026-01-01",
            last_updated="2026-01-02",
        )
        assert fm == (
            "---\n"
            "id: rag\n"
            'title: "검색 증강 생성"\n'
            'source_url: "http://x"\n'
            "tags: [llm, retrieval]\n"
            'created_at: "2026-01-01"\n'
            'last_updated: "2026-01-02"\n'
            "---\n"
        )

    def test_empty_tags_inline(self):
        fm = build_frontmatter("x", "T", "", [], "2026-01-01", "2026-01-01")
        assert "tags: []\n" in fm

    def test_extra_fields_appended_and_standard_dropped(self):
        fm = build_frontmatter(
            "x", "T", "", ["t"], "2026-01-01", "2026-01-01",
            extra={"status": "draft", "id": "SHOULD_BE_DROPPED"},
        )
        assert 'status: "draft"\n' in fm
        assert "SHOULD_BE_DROPPED" not in fm

    def test_title_escapes_quote(self):
        fm = build_frontmatter('x', 'a"b', "", [], "2026-01-01", "2026-01-01")
        assert 'title: "a\\"b"\n' in fm


# ─── _build_note_path (concepts 중복 방지 회귀) ──────────────────────
class TestBuildNotePath:
    def test_plain_domain(self):
        assert _build_note_path("llm", "rag") == "llm/concepts/rag.md"

    def test_domain_ending_in_subtype_not_doubled(self):
        # 회귀: domain="concepts" → "concepts/concepts/" 이중화 금지
        assert _build_note_path("concepts", "rag") == "concepts/rag.md"

    def test_nested_domain(self):
        assert _build_note_path("a/b", "rag") == "a/b/concepts/rag.md"

    def test_empty_domain_defaults_to_subtype(self):
        assert _build_note_path("", "rag") == "concepts/rag.md"

    def test_strips_slashes(self):
        assert _build_note_path("/llm/", "rag") == "llm/concepts/rag.md"


# ─── _fill_sections ──────────────────────────────────────────────────
class TestFillSections:
    def test_replaces_matching_section_body(self):
        scaffold = "## 정의\n\n기존\n\n## 예시\n\n기존예시\n"
        out = _fill_sections(scaffold, {"정의": "새 정의"})
        assert "새 정의" in out
        assert "기존예시" in out  # 미지정 섹션은 보존

    def test_preserves_links_section(self):
        scaffold = "## 정의\n\n낡음\n\n## 관련\n\n{{links}}\n"
        out = _fill_sections(scaffold, {"정의": "X"})
        assert "{{links}}" in out


# ─── 템플릿 로드 ─────────────────────────────────────────────────────
class TestTemplates:
    def _make_template(self, wiki, name, text):
        tdir = wiki.parent / "templates"
        tdir.mkdir(exist_ok=True)
        (tdir / f"{name}.md").write_text(text, encoding="utf-8")
        return tdir

    def test_list_templates_sorted(self, wiki):
        tdir = self._make_template(wiki, "b", "x")
        (tdir / "a.md").write_text("y", encoding="utf-8")
        assert list_templates(tdir) == ["a", "b"]

    def test_list_templates_none_dir(self):
        assert list_templates(None) == []

    def test_load_template_extra_and_body(self, wiki):
        tdir = self._make_template(
            wiki, "concept",
            "---\nid: x\nstatus: draft\ntags: [seed]\n---\n## 정의\n\n{{body}}\n",
        )
        t = load_template(tdir, "concept")
        assert t is not None
        assert t["extra"] == {"status": "draft"}  # 표준 id는 제외
        assert t["tags"] == ["seed"]
        assert "## 정의" in t["body"]

    def test_load_template_missing_returns_none(self, wiki):
        tdir = self._make_template(wiki, "x", "y")
        assert load_template(tdir, "nope") is None


# ─── create_note (통합: 디스크 쓰기 + rescan) ────────────────────────
class TestCreateNote:
    def test_creates_file_with_explicit_domain(self, idx, graph):
        r = create_note(
            idx, graph, id="rag", title="RAG", body="본문", tags=["llm"], domain="llm",
        )
        assert r["status"] == "ok"
        assert r["relative_path"] == "llm/concepts/rag.md"
        assert (idx.wiki_path / "llm/concepts/rag.md").exists()
        assert "rag" in idx.all_ids  # build() 후 인덱스 반영

    def test_default_domain_concepts_when_no_related(self, idx, graph):
        # search_hybrid 빈 결과 → concepts 폴백 (이중화 없음)
        r = create_note(idx, graph, id="x", title="X", body="b", tags=[])
        assert r["relative_path"] == "concepts/x.md"

    def test_hangul_id_rejected(self, idx, graph):
        r = create_note(idx, graph, id="한글", title="X", body="b", tags=[])
        assert r["status"] == "error"
        assert "한글" in r["error"]

    def test_conflict_without_force(self, idx, graph):
        create_note(idx, graph, id="rag", title="RAG", body="b", tags=[], domain="llm")
        r = create_note(idx, graph, id="rag", title="RAG2", body="c", tags=[], domain="llm")
        assert r["status"] == "error"
        assert "충돌" in r["error"]

    def test_force_overwrites_with_backup(self, idx, graph):
        create_note(idx, graph, id="rag", title="RAG", body="b", tags=[], domain="llm")
        r = create_note(
            idx, graph, id="rag", title="RAG2", body="c", tags=[], domain="llm", force=True,
        )
        assert r["status"] == "ok"
        assert r["backup"] is not None

    def test_dry_run_no_write(self, idx, graph):
        r = create_note(
            idx, graph, id="rag", title="RAG", body="b", tags=[], domain="llm", dry_run=True,
        )
        assert r["status"] == "preview"
        assert not (idx.wiki_path / "llm/concepts/rag.md").exists()

    def test_broken_link_warning(self, idx, graph):
        r = create_note(
            idx, graph, id="rag", title="RAG", body="b", tags=[],
            domain="llm", links=["nonexistent"],
        )
        assert r["status"] == "ok"
        assert any("broken" in w for w in r["warnings"])

    def test_template_sections_applied(self, idx, graph, wiki):
        tdir = wiki.parent / "templates"
        tdir.mkdir(exist_ok=True)
        (tdir / "concept.md").write_text(
            "---\nstatus: draft\n---\n## 정의\n\n채워질예정\n\n## 관련\n\n{{links}}\n",
            encoding="utf-8",
        )
        r = create_note(
            idx, graph, id="rag", title="RAG", body="", tags=[], domain="llm",
            template="concept", template_dir=tdir, sections={"정의": "RAG란..."},
        )
        assert r["status"] == "ok"
        text = (idx.wiki_path / r["relative_path"]).read_text(encoding="utf-8")
        assert "RAG란..." in text
        assert 'status: "draft"' in text


# ─── update / add_link / delete 흐름 ─────────────────────────────────
class TestNoteLifecycle:
    def _seed(self, idx, graph, nid, **kw):
        return create_note(idx, graph, id=nid, title=kw.get("title", nid),
                           body=kw.get("body", "본문"), tags=kw.get("tags", []),
                           domain="llm", links=kw.get("links"))

    def test_update_body_and_last_updated(self, idx, graph):
        self._seed(idx, graph, "rag")
        r = update_note(idx, id="rag", body="새 본문")
        assert r["status"] == "ok"
        text = (idx.wiki_path / idx.notes["rag"]["path"]).read_text(encoding="utf-8")
        assert "새 본문" in text

    def test_update_append(self, idx, graph):
        self._seed(idx, graph, "rag", body="A")
        update_note(idx, id="rag", body="B", append=True)
        text = (idx.wiki_path / idx.notes["rag"]["path"]).read_text(encoding="utf-8")
        assert "A" in text and "B" in text

    def test_update_missing_note(self, idx, graph):
        r = update_note(idx, id="nope", body="x")
        assert r["status"] == "error"

    def test_add_link_bidirectional_manual(self, idx, graph):
        self._seed(idx, graph, "rag")
        self._seed(idx, graph, "embedding")
        r = add_link(idx, id="rag", target_id="embedding")
        assert r["status"] == "ok"
        text = (idx.wiki_path / idx.notes["rag"]["path"]).read_text(encoding="utf-8")
        assert "[[embedding" in text

    def test_add_link_to_missing_target_rejected(self, idx, graph):
        self._seed(idx, graph, "rag")
        r = add_link(idx, id="rag", target_id="ghost")
        assert r["status"] == "error"
        assert "broken" in r["error"]

    def test_add_link_idempotent(self, idx, graph):
        self._seed(idx, graph, "rag")
        self._seed(idx, graph, "embedding")
        add_link(idx, id="rag", target_id="embedding")
        r = add_link(idx, id="rag", target_id="embedding")
        assert r.get("already_present") is True

    def test_delete_requires_confirm(self, idx, graph):
        self._seed(idx, graph, "rag")
        r = delete_note(idx, id="rag")
        assert r["status"] == "error"

    def test_delete_with_confirm(self, idx, graph):
        self._seed(idx, graph, "rag")
        path = idx.wiki_path / idx.notes["rag"]["path"]
        r = delete_note(idx, id="rag", confirm=True)
        assert r["status"] == "ok"
        assert not path.exists()
        assert "rag" not in idx.all_ids

    def test_delete_reports_inbound_breaks(self, idx, graph):
        self._seed(idx, graph, "embedding")
        self._seed(idx, graph, "rag")
        add_link(idx, id="rag", target_id="embedding")  # rag → embedding
        r = delete_note(idx, id="embedding", confirm=True)
        assert "rag" in r["inbound_links_broken"]
