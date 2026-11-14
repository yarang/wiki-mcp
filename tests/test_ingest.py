"""ingest.py characterization 테스트 — JSON 방어·청킹·재귀 스캔·주입 LLM 인제스트."""
from __future__ import annotations

import json

import pytest

from wiki_mcp.ingest import (
    build_prompts,
    filter_allowed_links,
    ingest_file,
    ingest_long_text,
    ingest_text,
    is_ingestable,
    iter_raw_files,
    outline_document,
    parse_note_json,
    related_candidates,
    split_into_windows,
    template_section_headings,
    validate_evidence,
)

from conftest import echo_note


# ─── parse_note_json (코드펜스/설명문 방어) ──────────────────────────
class TestParseNoteJson:
    def test_plain_object(self):
        assert parse_note_json('{"id": "x"}') == {"id": "x"}

    def test_strips_code_fence(self):
        assert parse_note_json('```json\n{"id": "x"}\n```') == {"id": "x"}

    def test_ignores_surrounding_prose(self):
        assert parse_note_json('네, 결과입니다:\n{"id": "x"}\n감사합니다') == {"id": "x"}

    def test_no_object_raises(self):
        with pytest.raises(ValueError, match="JSON 객체를 찾지 못함"):
            parse_note_json("객체 없음")

    def test_malformed_raises(self):
        with pytest.raises(ValueError, match="파싱 실패"):
            parse_note_json('{"id": }')

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_note_json("")


class TestEvidenceValidation:
    def test_accepts_verbatim_quote_with_whitespace_normalization(self):
        assert validate_evidence("정확한 원문\n근거 문장입니다.", ["정확한 원문 근거 문장입니다."])

    def test_rejects_hallucinated_quote(self):
        with pytest.raises(ValueError, match="존재하지 않는 근거"):
            validate_evidence("실제 원문 내용입니다.", ["원문에는 없는 임의의 주장입니다."])

    def test_rejects_missing_evidence(self):
        with pytest.raises(ValueError, match="근거.*없음"):
            validate_evidence("실제 원문 내용입니다.", [])


class TestRelatedCandidates:
    def test_uses_local_search_and_limits_candidates(self, idx, wiki):
        for nid, title in (("related", "관련 문서"), ("other", "기타 문서")):
            (wiki / f"{nid}.md").write_text(
                f"---\nid: {nid}\ntitle: {title}\ntags: [monitoring]\n---\n본문",
                encoding="utf-8",
            )
        idx.build()
        idx.search_results = [{"id": "related", "title": "관련 문서", "tags": ["monitoring"]}]

        candidates = related_candidates(idx, "모니터링 원문", limit=1)

        assert [candidate["id"] for candidate in candidates] == ["related"]
        assert candidates[0]["title"] == "관련 문서"

    def test_filters_links_outside_allowlist(self):
        accepted, rejected = filter_allowed_links(
            ["related", "invented", "related"], {"related"}
        )
        assert accepted == ["related"]
        assert rejected == ["invented"]


# ─── template_section_headings ───────────────────────────────────────
class TestTemplateSectionHeadings:
    def test_collects_h2_h3(self):
        s = "## 정의\n내용\n### 세부\n내용\n"
        assert template_section_headings(s) == ["정의", "세부"]

    def test_excludes_links_section(self):
        s = "## 정의\n내용\n## 관련\n{{links}}\n"
        assert template_section_headings(s) == ["정의"]


# ─── build_prompts ───────────────────────────────────────────────────
class TestBuildPrompts:
    def test_body_mode_schema(self):
        sys, user = build_prompts("원문", ["a", "b"])
        assert '"body"' in sys
        assert "a\nb" in user
        assert "원문" in user

    def test_section_mode_schema(self):
        sys, user = build_prompts("원문", [], ["정의", "예시"])
        assert "sections" in sys
        assert '"정의"' in sys and '"예시"' in sys

    def test_no_candidates_shows_none(self):
        sys, user = build_prompts("원문", [])
        assert "(없음)" in user


# ─── split_into_windows (청킹 핵심) ──────────────────────────────────
class TestSplitIntoWindows:
    def test_short_text_single_window(self):
        assert split_into_windows("짧은 글", 1000) == ["짧은 글"]

    def test_empty_returns_empty(self):
        assert split_into_windows("   ", 1000) == []

    def test_zero_maxchars_single(self):
        assert split_into_windows("아무거나", 0) == ["아무거나"]

    def test_splits_on_headings(self):
        text = "# A\n" + "x" * 40 + "\n# B\n" + "y" * 40
        windows = split_into_windows(text, 50)
        assert len(windows) >= 2
        assert any("A" in w for w in windows)
        assert any("B" in w for w in windows)

    def test_each_window_within_limit_when_possible(self):
        # 헤딩으로 나뉘는 문서는 각 창이 max_chars 근처를 넘지 않게 병합
        text = "\n".join(f"## S{i}\n" + "본문 " * 10 for i in range(6))
        windows = split_into_windows(text, 120)
        assert all(w.strip() for w in windows)
        # 병합 결과가 원문 개념 수(6)보다 과도하게 쪼개지지 않음
        assert len(windows) <= 6

    def test_large_paragraph_split(self):
        # 단일 헤딩 아래 거대한 문단 → 문단(\n\n) 단위 재분할
        text = "## H\n\n" + "\n\n".join("문단내용" * 20 for _ in range(5))
        windows = split_into_windows(text, 100)
        assert len(windows) >= 2


# ─── is_ingestable / iter_raw_files (재귀 스캔 회귀) ─────────────────
EXTS = [".md", ".txt", ".pdf"]


class TestIsIngestable:
    def test_supported_ext(self, tmp_path):
        assert is_ingestable(tmp_path / "a.md", EXTS) is True

    def test_unsupported_ext(self, tmp_path):
        assert is_ingestable(tmp_path / "a.png", EXTS) is False

    def test_hidden_file(self, tmp_path):
        assert is_ingestable(tmp_path / ".hidden.md", EXTS) is False

    def test_readme_excluded(self, tmp_path):
        assert is_ingestable(tmp_path / "README.md", EXTS) is False

    def test_processed_subdir_excluded(self, tmp_path):
        raw = tmp_path / "raw"
        f = raw / ".processed" / "a.md"
        assert is_ingestable(f, EXTS, raw_dir=raw) is False

    def test_nested_visible_subdir_included(self, tmp_path):
        raw = tmp_path / "raw"
        f = raw / "anthropic" / "api" / "a.md"
        assert is_ingestable(f, EXTS, raw_dir=raw) is True


class TestIterRawFiles:
    def test_recursive_scan(self, tmp_path):
        # 회귀: raw/anthropic/**/*.md 가 재귀로 잡혀야 함
        raw = tmp_path / "raw"
        (raw / "anthropic" / "api").mkdir(parents=True)
        (raw / "top.md").write_text("x", encoding="utf-8")
        (raw / "anthropic" / "guide.md").write_text("x", encoding="utf-8")
        (raw / "anthropic" / "api" / "deep.md").write_text("x", encoding="utf-8")
        found = {p.name for p in iter_raw_files(raw, EXTS)}
        assert found == {"top.md", "guide.md", "deep.md"}

    def test_excludes_processed_and_readme(self, tmp_path):
        raw = tmp_path / "raw"
        (raw / ".processed").mkdir(parents=True)
        (raw / ".processed" / "old.md").write_text("x", encoding="utf-8")
        (raw / "README.md").write_text("x", encoding="utf-8")
        (raw / "real.md").write_text("x", encoding="utf-8")
        found = {p.name for p in iter_raw_files(raw, EXTS)}
        assert found == {"real.md"}

    def test_missing_dir_empty(self, tmp_path):
        assert iter_raw_files(tmp_path / "nope", EXTS) == []

    def test_none_dir_empty(self):
        assert iter_raw_files(None, EXTS) == []


# ─── ingest_text (주입 LLM) ──────────────────────────────────────────
class TestIngestText:
    def test_creates_note_from_llm_json(self, idx, graph):
        fn = echo_note({"id": "rag", "title": "RAG", "body": "본문", "tags": ["llm"], "links": []})
        r = ingest_text(idx, graph, "원문", complete_fn=fn)
        assert r["status"] == "ok"
        assert "rag" in idx.all_ids

    def test_missing_id_errors(self, idx, graph):
        fn = echo_note({"title": "no id", "body": "b"})
        r = ingest_text(idx, graph, "원문", complete_fn=fn)
        assert r["status"] == "error"
        assert "id 없음" in r["error"]

    def test_id_not_in_tags_rule_present(self, idx, graph):
        # 태그 오염(id를 tags에 넣음) 방지 규칙이 프롬프트에 있어야 함
        captured = {}

        def fn(system, user):
            captured["system"] = system
            return json.dumps({"id": "rag", "title": "R", "body": "b", "tags": ["llm"]})

        ingest_text(idx, graph, "원문", complete_fn=fn)
        assert "id를 tags에 넣지 마라" in captured["system"]

    def test_rejects_llm_link_outside_retrieved_candidates(self, idx, graph, wiki):
        (wiki / "concepts").mkdir()
        (wiki / "concepts" / "related.md").write_text(
            "---\nid: related\ntitle: 관련 노트\ntags: [rag]\n---\n관련 본문",
            encoding="utf-8",
        )
        idx.build()
        idx.search_results = [{"id": "related", "title": "관련 노트", "tags": ["rag"]}]
        fn = echo_note({
            "id": "new-note", "title": "새 노트", "body": "본문", "tags": ["rag"],
            "links": ["related", "invented-note"],
        })

        r = ingest_text(idx, graph, "RAG 관련 원문", complete_fn=fn)

        assert r["status"] == "ok"
        assert "related" in idx.out_links["new-note"]
        assert "invented-note" not in idx.out_links["new-note"]
        assert any("invented-note" in warning for warning in r["warnings"])


# ─── outline_document / ingest_long_text (긴 문서 → 다중 노트) ───────
class TestLongIngest:
    def test_outline_then_write_creates_multiple(self, idx, graph):
        long_text = "\n".join(f"## 개념{i}\n" + ("설명 " * 40) for i in range(3))

        def fn(system, user):
            if "나누는 편집자" in system:  # 아웃라인 단계
                return json.dumps({"notes": [
                    {"id": "concept-a", "title": "A", "scope": "A"},
                    {"id": "concept-b", "title": "B", "scope": "B"},
                ]})
            # 본문 작성 단계
            return json.dumps({"tags": ["t"], "links": [], "body": "본문"})

        r = ingest_long_text(idx, graph, long_text, complete_fn=fn, max_chars=200, max_notes=10)
        assert r["status"] == "ok"
        assert set(r["created"]) == {"concept-a", "concept-b"}

    def test_no_plan_errors(self, idx, graph):
        def fn(system, user):
            return json.dumps({"notes": []})

        r = ingest_long_text(idx, graph, "짧은데 " * 50, complete_fn=fn, max_chars=50)
        assert r["status"] == "error"

    def test_outline_dedupes_ids(self, idx, graph):
        text = "\n".join(f"## S{i}\n" + ("본문 " * 40) for i in range(3))

        def fn(system, user):
            return json.dumps({"notes": [{"id": "dup", "title": "D", "scope": "x"}]})

        planned, windows = outline_document(text, [], fn, max_chars=120, max_notes=10)
        ids = [p["id"] for p in planned]
        assert ids.count("dup") == 1  # 창마다 같은 id 반환해도 1개만

    def test_outline_respects_max_notes(self, idx, graph):
        text = "\n".join(f"## S{i}\n" + ("본문 " * 40) for i in range(5))
        counter = {"n": 0}

        def fn(system, user):
            counter["n"] += 1
            return json.dumps({"notes": [
                {"id": f"n{counter['n']}-a", "title": "A", "scope": "x"},
                {"id": f"n{counter['n']}-b", "title": "B", "scope": "x"},
            ]})

        planned, _ = outline_document(text, [], fn, max_chars=120, max_notes=3)
        assert len(planned) <= 3

    def test_outline_skips_heading_only_window(self, idx, graph):
        # 내용 없는 헤딩만 있는 창은 LLM 호출 낭비 → 건너뜀
        calls = {"n": 0}

        def fn(system, user):
            calls["n"] += 1
            return json.dumps({"notes": []})

        outline_document("# 제목만\n## 소제목만\n", [], fn, max_chars=10, max_notes=5)
        assert calls["n"] == 0


# ─── 프롬프트 규칙 회귀 (과분할·태그 오염 방지 튜닝 고정) ────────────
class TestPromptRules:
    """프롬프트 튜닝으로 해결한 품질 문제가 되돌아가지 않도록 규칙 존재를 고정.

    배경: 아웃라인이 2개 개념을 8개 노트로 과분할하고, 노트 id가 tags에
    섞여 들어가던 문제를 프롬프트 규칙 추가로 해결했다.
    """

    def _capture_outline_system(self, idx, graph) -> str:
        captured = {}

        def fn(system, user):
            if "나누는 편집자" in system:
                captured["outline"] = system
            return json.dumps({"notes": []})

        outline_document("## 개념\n" + ("본문 " * 40), [], fn, max_chars=100, max_notes=5)
        return captured.get("outline", "")

    def test_outline_forbids_splitting_one_concept(self, idx, graph):
        s = self._capture_outline_system(idx, graph)
        assert "쪼개지 마라" in s

    def test_outline_maps_subheading_to_one_note(self, idx, graph):
        s = self._capture_outline_system(idx, graph)
        assert "소제목(## ) 하나가 노트 하나에 대응" in s

    def test_outline_merges_overlapping(self, idx, graph):
        s = self._capture_outline_system(idx, graph)
        assert "겹치는 항목은 하나로 합친다" in s

    def test_outline_enforces_max_notes(self, idx, graph):
        s = self._capture_outline_system(idx, graph)
        assert "최대 5개" in s

    def test_write_prompt_forbids_id_in_tags(self, idx, graph):
        captured = {}

        def fn(system, user):
            if "나누는 편집자" in system:
                return json.dumps({"notes": [{"id": "a", "title": "A", "scope": "x"}]})
            captured["write"] = system
            return json.dumps({"tags": ["t"], "links": [], "body": "b"})

        ingest_long_text(idx, graph, "## 개념\n" + ("본문 " * 40),
                         complete_fn=fn, max_chars=100)
        assert "id를 tags에 넣지 마라" in captured["write"]

    def test_write_prompt_scopes_to_single_topic(self, idx, graph):
        captured = {}

        def fn(system, user):
            if "나누는 편집자" in system:
                return json.dumps({"notes": [
                    {"id": "a", "title": "A제목", "scope": "A범위"},
                ]})
            captured["write"] = system
            return json.dumps({"tags": ["t"], "links": [], "body": "b"})

        ingest_long_text(idx, graph, "## 개념\n" + ("본문 " * 40),
                         complete_fn=fn, max_chars=100)
        assert "이 주제만 다루고 다른 주제는 제외한다" in captured["write"]
        assert "A범위" in captured["write"]  # scope가 작성 프롬프트로 전달됨


# ─── ingest_file (watch 데몬 경로: 추출 → 노트 → 원문 이동) ──────────
class TestIngestFile:
    def _dirs(self, tmp_path):
        processed = tmp_path / ".processed"
        failed = tmp_path / ".failed"
        return processed, failed

    def test_success_moves_to_processed(self, idx, graph, tmp_path):
        raw = tmp_path / "a.md"
        raw.write_text("충분히 긴 원문 내용입니다. 노트로 변환됩니다.", encoding="utf-8")
        processed, failed = self._dirs(tmp_path)
        fn = echo_note({
            "id": "rag", "title": "RAG", "body": "본문", "tags": ["llm"],
            "evidence": ["충분히 긴 원문 내용입니다."],
        })

        r = ingest_file(idx, graph, raw, complete_fn=fn,
                        processed_dir=processed, failed_dir=failed)
        assert r["status"] == "ok"
        assert not raw.exists()
        assert (processed / "a.md").exists()
        assert "rag" in idx.all_ids
        assert r["source_id"] in idx.all_ids
        assert f"[[{r['source_id']}" in idx.notes["rag"]["content"]
        assert "> 충분히 긴 원문 내용입니다." in idx.notes["rag"]["content"]

    def test_failure_moves_to_failed(self, idx, graph, tmp_path):
        raw = tmp_path / "a.md"
        raw.write_text("충분히 긴 원문 내용입니다. 변환은 실패합니다.", encoding="utf-8")
        processed, failed = self._dirs(tmp_path)

        def fn(system, user):
            return "JSON 아님 — 파싱 실패 유도"

        r = ingest_file(idx, graph, raw, complete_fn=fn,
                        processed_dir=processed, failed_dir=failed)
        assert r["status"] == "error"
        assert not raw.exists()
        assert (failed / "a.md").exists()

    def test_empty_file_fails(self, idx, graph, tmp_path):
        raw = tmp_path / "empty.md"
        raw.write_text("   ", encoding="utf-8")
        processed, failed = self._dirs(tmp_path)
        fn = echo_note({"id": "x", "title": "X", "body": "b"})

        r = ingest_file(idx, graph, raw, complete_fn=fn,
                        processed_dir=processed, failed_dir=failed)
        assert r["status"] == "error"
        assert "빈 파일" in r["error"]
        assert (failed / "empty.md").exists()

    def test_image_without_ocr_reports_extraction_error(self, idx, graph, tmp_path):
        raw = tmp_path / "scan.png"
        raw.write_bytes(b"\x89PNG\r\n")
        processed, failed = self._dirs(tmp_path)
        fn = echo_note({"id": "x", "title": "X", "body": "b"})

        r = ingest_file(idx, graph, raw, complete_fn=fn, ocr=False,
                        processed_dir=processed, failed_dir=failed)
        assert r["status"] == "error"
        assert "추출 실패" in r["error"]
        assert (failed / "scan.png").exists()

    def test_long_text_routes_to_multi_note(self, idx, graph, tmp_path):
        raw = tmp_path / "long.md"
        raw.write_text("\n".join(f"## 개념{i}\n" + ("설명 " * 40) for i in range(3)),
                       encoding="utf-8")
        processed, failed = self._dirs(tmp_path)

        def fn(system, user):
            if "나누는 편집자" in system:
                return json.dumps({"notes": [
                    {"id": "c-a", "title": "A", "scope": "A"},
                    {"id": "c-b", "title": "B", "scope": "B"},
                ]})
            return json.dumps({
                "tags": ["t"], "links": [], "body": "본문",
                "evidence": ["설명 설명 설명 설명"],
            })

        r = ingest_file(idx, graph, raw, complete_fn=fn, max_chars=200, max_notes=10,
                        processed_dir=processed, failed_dir=failed)
        assert r["status"] == "ok"
        assert set(r["created"]) == {"c-a", "c-b"}
        assert (processed / "long.md").exists()

    def test_short_text_routes_to_single_note(self, idx, graph, tmp_path):
        raw = tmp_path / "short.md"
        raw.write_text("짧은 원문 내용입니다. 노트 하나면 충분합니다.", encoding="utf-8")
        fn = echo_note({
            "id": "single", "title": "S", "body": "본문",
            "evidence": ["짧은 원문 내용입니다."],
        })

        r = ingest_file(idx, graph, raw, complete_fn=fn, max_chars=6000)
        assert r["status"] == "ok"
        assert r["id"] == "single"  # 다중 노트 경로가 아님(created 키 없음)
        assert "created" not in r

    def test_no_dest_dirs_leaves_raw_in_place(self, idx, graph, tmp_path):
        raw = tmp_path / "a.md"
        raw.write_text("충분히 긴 원문 내용입니다. 이동 없이 유지.", encoding="utf-8")
        fn = echo_note({
            "id": "rag", "title": "RAG", "body": "본문",
            "evidence": ["충분히 긴 원문 내용입니다."],
        })

        r = ingest_file(idx, graph, raw, complete_fn=fn)
        assert r["status"] == "ok"
        assert raw.exists()  # processed_dir 미지정 → 원문 유지

    def test_hallucinated_evidence_rejects_derived_but_preserves_source(self, idx, graph, tmp_path):
        raw = tmp_path / "accuracy.md"
        raw.write_text("검증 가능한 실제 원문 내용입니다.", encoding="utf-8")
        fn = echo_note({
            "id": "invented", "title": "임의 작성", "body": "원문 밖 주장",
            "evidence": ["원문에는 존재하지 않는 조작된 근거입니다."],
        })

        r = ingest_file(idx, graph, raw, complete_fn=fn)

        assert r["status"] == "error"
        assert "존재하지 않는 근거" in r["error"]
        assert "invented" not in idx.all_ids
        assert r["source_id"] in idx.all_ids
        assert "검증 가능한 실제 원문 내용입니다." in idx.notes[r["source_id"]]["content"]
