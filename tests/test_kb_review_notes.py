"""KB 노트 파일 연산 — find_notes·note_content_hash·set_frontmatter_key·
render_sources_section·replace_note_body (설계 2.7절, 내부 공용).
"""

from __future__ import annotations

import pytest

from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.notes import (
    find_notes,
    note_content_hash,
    render_sources_section,
    replace_note_body,
    set_frontmatter_key,
)


def _note_text(**extra: str) -> str:
    lines = [
        "---",
        "id: sample-note",
        'title: "견본 노트"',
        'source_url: ""',
        "tags: [a, b]",
        'created_at: "2026-01-01"',
        'last_updated: "2026-01-01"',
    ]
    lines.extend(f"{k}: {v}" for k, v in extra.items())
    lines.extend(["---", "", "본문 내용이다.", "", "## 관련 노트", "", "- [[other]]", ""])
    return "\n".join(lines)


class TestFindNotes:
    def test_maps_id_to_posix_paths(self, tmp_path):
        (tmp_path / "concepts").mkdir()
        (tmp_path / "concepts" / "a.md").write_text(_note_text(), encoding="utf-8")
        found = find_notes(tmp_path)
        assert found == {"sample-note": ["concepts/a.md"]}

    def test_skips_hidden_dirs_and_idless_notes(self, tmp_path):
        (tmp_path / ".drafts").mkdir()
        (tmp_path / ".drafts" / "a.md").write_text(_note_text(), encoding="utf-8")
        (tmp_path / "no-id.md").write_text("머리말 없는 문서 본문.\n", encoding="utf-8")
        assert find_notes(tmp_path) == {}

    def test_two_files_same_id_listed_in_order(self, tmp_path):
        for sub in ("a", "z"):
            (tmp_path / sub).mkdir()
            (tmp_path / sub / "note.md").write_text(_note_text(), encoding="utf-8")
        found = find_notes(tmp_path)
        assert found == {"sample-note": ["a/note.md", "z/note.md"]}


class TestNoteContentHash:
    def test_kb_source_status_line_does_not_change_hash(self):
        a = note_content_hash(_note_text())
        b = note_content_hash(_note_text(**{"kb_source_status": '"missing"'}))
        assert a == b

    def test_crlf_equals_lf(self):
        assert note_content_hash(_note_text()) == note_content_hash(
            _note_text().replace("\n", "\r\n")
        )

    def test_body_change_changes_hash(self):
        changed = _note_text().replace("본문 내용이다.", "본문이 바뀌었다.")
        assert note_content_hash(_note_text()) != note_content_hash(changed)

    def test_plain_text_without_frontmatter_hashes_whole_text(self):
        # 머리말 규칙에 맞지 않는 텍스트 — 전체 텍스트 기준 해시(라인 정규화만)
        assert note_content_hash("그냥 본문\n") == note_content_hash("그냥 본문\n")
        assert note_content_hash("그냥 본문\n") != note_content_hash("다른 본문\n")


class TestSetFrontmatterKey:
    def test_set_new_key_before_closing_delimiter(self):
        text = _note_text()
        out = set_frontmatter_key(text, "kb_source_status", '"missing"')
        assert 'kb_source_status: "missing"' in out
        # 닫는 --- 바로 앞에 들어간다
        head = out.split("\n---\n", 1)[0].splitlines()[-1]
        assert head == 'kb_source_status: "missing"'

    def test_replace_existing_key(self):
        text = _note_text(**{"kb_source_status": '"changed"'})
        out = set_frontmatter_key(text, "kb_source_status", '"missing"')
        assert 'kb_source_status: "missing"' in out
        assert '"changed"' not in out

    def test_remove_key(self):
        text = _note_text(**{"kb_source_status": '"missing"'})
        out = set_frontmatter_key(text, "kb_source_status", None)
        assert "kb_source_status" not in out

    def test_other_keys_survive_including_user_keys(self):
        text = _note_text(**{"category": '"x"'})
        out = set_frontmatter_key(text, "kb_source_status", '"missing"')
        assert 'category: "x"' in out
        assert out.count("id: sample-note") == 1

    def test_block_list_key_survives_edits_of_other_keys(self):
        text = '---\nid: sample-note\ntags:\n  - a\n  - b\ncategory: "x"\n---\n본문.\n'
        out = set_frontmatter_key(text, "kb_source_status", '"missing"')
        import yaml

        meta = yaml.safe_load(out.split("\n---\n", 1)[0])
        assert meta["tags"] == ["a", "b"]
        assert meta["category"] == "x"

    def test_multiline_block_value_is_replaced_with_single_line(self):
        """R2 — 편집기가 kb_sources 를 블록 YAML 로 다시 썐 경우도 키 교체가 된다."""
        text = (
            "---\nid: sample-note\nkb_sources:\n- source_id: src-1\n  rel_path: a.md\n---\n본문.\n"
        )
        out = set_frontmatter_key(text, "kb_sources", '[{"source_id": "src-1"}]')
        import yaml

        meta = yaml.safe_load(out.split("\n---\n", 1)[0])
        assert meta["kb_sources"] == [{"source_id": "src-1"}]

    def test_crlf_input_handled(self):
        text = _note_text().replace("\n", "\r\n")
        out = set_frontmatter_key(text, "kb_source_status", '"missing"')
        assert 'kb_source_status: "missing"' in out

    def test_missing_frontmatter_raises(self):
        with pytest.raises(KBError) as e:
            set_frontmatter_key("머리말 없는 본문.\n", "kb_source_status", '"missing"')
        assert e.value.code == "note_frontmatter_unsupported"

    def test_broken_frontmatter_raises(self):
        # 닫는 --- 이 없는 머리말
        text = "---\nid: sample-note\ntitle: x\n\n본문만 있다.\n"
        with pytest.raises(KBError) as e:
            set_frontmatter_key(text, "kb_source_status", '"missing"')
        assert e.value.code == "note_frontmatter_unsupported"


class TestReplaceNoteBody:
    def test_replaces_body_keeping_frontmatter_bytes(self):
        text = _note_text(**{"category": '"x"'})
        out = replace_note_body(text, "새 본문이다.\n")
        assert out.startswith("---\n")
        assert 'category: "x"' in out
        assert out.endswith("---\n\n새 본문이다.\n")

    def test_missing_frontmatter_raises(self):
        with pytest.raises(KBError) as e:
            replace_note_body("본문만 있다.\n", "새 본문\n")
        assert e.value.code == "note_frontmatter_unsupported"


class TestRenderSourcesSection:
    def test_renders_markers_and_quotes(self):
        section = render_sources_section(
            [
                {
                    "rel_path": "규정/출장.hwpx",
                    "source_id": "src-20261115-001",
                    "evidence": [
                        {"quote": "국내 출장 일비는 하루 2만 원으로 한다", "location": "section 0"},
                        {"quote": "두 번째 구절 인용이다", "location": None},
                    ],
                }
            ]
        )
        lines = section.splitlines()
        assert lines[0] == "<!-- kb:sources:start -->"
        assert lines[-1] == "<!-- kb:sources:end -->"
        assert "## 출처" in section
        assert "- `규정/출장.hwpx` (src-20261115-001)" in section
        assert '  - section 0: "국내 출장 일비는 하루 2만 원으로 한다"' in section
        assert '  - 위치 없음: "두 번째 구절 인용이다"' in section

    def test_backtick_in_rel_path_is_replaced(self):
        section = render_sources_section(
            [
                {
                    "rel_path": "a`b.md",
                    "source_id": "src-1",
                    "evidence": [{"quote": "구절 인용이다", "location": None}],
                }
            ]
        )
        assert "- `a'b.md` (src-1)" in section
        assert "`a`b.md`" not in section

    def test_empty_links_render_markers_only(self):
        section = render_sources_section([])
        assert section.startswith("<!-- kb:sources:start -->")
        assert section.rstrip().endswith("<!-- kb:sources:end -->")


# ─── 0.4.0 리뷰 반영 — 빈 줄 블록 보존 ────────────────────────────────
class TestKeyBlockBlankLines:
    TEXT = '---\nid: sample-note\ntitle: "x"\n\ncategory: "x"\n\n---\n본문.\n'

    def test_removal_deletes_key_line_only(self):
        """키를 지울 때 키 줄만 사라진다 — 뒤따르던 빈 줄은 그대로 남는다."""
        out = set_frontmatter_key(self.TEXT, "category", None)
        assert out == (
            "---\n"
            "id: sample-note\n"
            'title: "x"\n'
            "\n"
            "\n"  # 원래 키 앞뒤 빈 줄이 둘 다 남는다
            "---\n"
            "본문.\n"
        )

    def test_replacement_keeps_blank_lines(self):
        out = set_frontmatter_key(self.TEXT, "category", '"y"')
        assert 'category: "y"' in out
        # 새 키 줄은 닫는 --- 바로 앞(기존 규칙). 줄 수는 교체이므로 보존된다.
        assert out.count("\n") == self.TEXT.count("\n")
        assert "\n\n" in out  # 빈 줄이 지워지지 않았다
        import yaml

        meta = yaml.safe_load(out.split("\n---\n", 1)[0])
        assert meta["category"] == "y"
        assert meta["id"] == "sample-note"


class TestPreserveNewlines:
    def test_crlf_note_keeps_crlf(self):
        from wiki_mcp.kb.notes import preserve_newlines

        original = "---\r\nid: a\r\n---\r\n본문.\r\n"
        rewritten_lf = original.replace("\r\n", "\n")
        assert preserve_newlines(original, rewritten_lf) == original

    def test_lf_note_stays_lf(self):
        from wiki_mcp.kb.notes import preserve_newlines

        original = "---\nid: a\n---\n본문.\n"
        assert preserve_newlines(original, original) == original

    def test_mixed_note_normalizes_to_lf(self):
        """LF·CRLF 가 섞인 노트는 새 텍스트(LF)를 그대로 둔다."""
        from wiki_mcp.kb.notes import preserve_newlines

        mixed = "---\r\nid: a\n---\r\n본문.\n"
        rewritten = mixed.replace("\r\n", "\n")
        assert preserve_newlines(mixed, rewritten) == rewritten
