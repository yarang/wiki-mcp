"""KB 원본 관리 — add_source / list_sources / remove_source 계약.

경로 검증(거부 규칙), 민감 이름 경고, 원본 무결성(remove 가 원본을 건드리지
않는지)을 검증한다. 홈·시스템 폴더 판정은 home 경로를 주입해 결정적으로
검증한다(테스트에 시스템 절대 경로를 하드코딩하지 않는다).
"""

from __future__ import annotations

import os

import pytest
from kb_helper import future_now

from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.sources import add_source, list_sources, remove_source
from wiki_mcp.kb.store import KnowledgeBase


class TestInit:
    def test_init_creates_structure(self, kb_root):
        kb = KnowledgeBase(kb_root)
        out = kb.init()
        assert (kb_root / "sources.yaml").is_file()
        assert (kb_root / ".kb").is_dir()
        assert (kb_root / ".kb" / "extract").is_dir()
        assert (kb_root / "wiki").is_dir()
        assert out["kb_dir"] == str(kb_root)
        assert len(out["created"]) == 5

    def test_init_idempotent(self, kb_root):
        kb = KnowledgeBase(kb_root)
        kb.init()
        out = kb.init()
        assert out["created"] == []

    def test_require_initialized_rejects_empty_root(self, tmp_path):
        kb = KnowledgeBase(tmp_path / "nowhere")
        with pytest.raises(KBError) as ei:
            kb.require_initialized()
        assert ei.value.code == "kb_not_initialized"


class TestAddSource:
    def test_register_real_folder(self, kb, source_dir):
        out = add_source(kb, source_dir)
        src = out["source"]
        assert src["path"] == str(source_dir.resolve())
        assert src["id"].startswith("src-")
        assert src["include"] == [] and src["exclude"] == []
        assert src["added_at"]
        assert list_sources(kb)["sources"][0]["id"] == src["id"]

    def test_missing_path_rejected(self, kb, tmp_path):
        with pytest.raises(KBError) as ei:
            add_source(kb, tmp_path / "nope")
        assert ei.value.code == "source_not_found"

    def test_file_rejected(self, kb, tmp_path):
        f = tmp_path / "afile.txt"
        f.write_text("x", encoding="utf-8")
        with pytest.raises(KBError) as ei:
            add_source(kb, f)
        assert ei.value.code == "source_not_a_directory"

    def test_inside_kb_rejected(self, kb, kb_root):
        inside = kb_root / "wiki"
        with pytest.raises(KBError) as ei:
            add_source(kb, inside)
        assert ei.value.code == "overlaps_kb"

    def test_kb_parent_rejected(self, kb, kb_root):
        # KB 폴더를 포함하는 상위 폴더도 순환 위험이므로 거부
        with pytest.raises(KBError) as ei:
            add_source(kb, kb_root.parent)
        assert ei.value.code == "overlaps_kb"

    def test_overlap_with_existing_rejected(self, kb, source_dir, tmp_path):
        add_source(kb, source_dir)
        sub = source_dir / "sub"
        sub.mkdir()
        with pytest.raises(KBError) as ei:
            add_source(kb, sub)
        assert ei.value.code == "overlaps_existing"

    def test_parent_of_existing_rejected(self, kb, tmp_path):
        family = tmp_path / "family"
        child = family / "child"
        child.mkdir(parents=True)
        add_source(kb, child)
        with pytest.raises(KBError) as ei:
            add_source(kb, family)
        assert ei.value.code == "overlaps_existing"

    def test_home_root_rejected(self, kb, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        with pytest.raises(KBError) as ei:
            add_source(kb, home, home=home)
        assert ei.value.code == "system_folder"

    def test_system_folder_rejected(self, kb, tmp_path):
        # home 주입으로 "~/Library", "~/AppData" 규칙을 결정적으로 검증
        for name in ("Library", "AppData"):
            sysdir = tmp_path / name
            sysdir.mkdir()
            with pytest.raises(KBError) as ei:
                add_source(kb, sysdir, home=tmp_path)
            assert ei.value.code == "system_folder"

    def test_nested_inside_system_folder_rejected(self, kb, tmp_path):
        deep = tmp_path / "Library" / "Something"
        deep.mkdir(parents=True)
        with pytest.raises(KBError) as ei:
            add_source(kb, deep, home=tmp_path)
        assert ei.value.code == "system_folder"

    def test_sensitive_name_returns_warning_without_registering(self, kb, tmp_path):
        ssh = tmp_path / ".ssh"
        ssh.mkdir()
        out = add_source(kb, ssh, home=tmp_path)
        assert "warning" in out and "source" not in out
        assert out["warning"]["code"] == "sensitive_name"
        assert ".ssh" in out["warning"]["matched"]
        assert list_sources(kb)["sources"] == []

    def test_sensitive_korean_name_warns(self, kb, tmp_path):
        payroll = tmp_path / "급여"
        payroll.mkdir()
        out = add_source(kb, payroll, home=tmp_path)
        assert out["warning"]["code"] == "sensitive_name"
        assert "급여" in out["warning"]["matched"]

    def test_sensitive_name_confirmed_registers(self, kb, tmp_path):
        ssh = tmp_path / ".ssh"
        ssh.mkdir()
        out = add_source(kb, ssh, confirm=True, home=tmp_path)
        assert "source" in out
        assert list_sources(kb)["sources"][0]["path"] == str(ssh.resolve())

    def test_symlink_resolved_to_real_path(self, kb, source_dir, tmp_path):
        link = tmp_path / "link-to-docs"
        os.symlink(source_dir, link)
        out = add_source(kb, link)
        assert out["source"]["path"] == str(source_dir.resolve())

    def test_include_exclude_persisted(self, kb, source_dir):
        out = add_source(kb, source_dir, include=["*.hwpx"], exclude=["drafts/*"])
        src = out["source"]
        assert src["include"] == ["*.hwpx"]
        assert src["exclude"] == ["drafts/*"]


class TestRemoveSource:
    def test_remove_deletes_only_kb_side(self, kb, source_dir, tmp_path):
        src = add_source(kb, source_dir)["source"]
        marker = source_dir / "keep.txt"
        marker.write_text("원본은 지워지지 않는다", encoding="utf-8")
        before = sorted(p.name for p in source_dir.iterdir())

        out = remove_source(kb, src["id"])
        assert out["removed"] == src["id"]
        assert list_sources(kb)["sources"] == []
        assert sorted(p.name for p in source_dir.iterdir()) == before
        assert marker.read_text(encoding="utf-8") == "원본은 지워지지 않는다"

    def test_remove_unknown_id(self, kb):
        with pytest.raises(KBError) as ei:
            remove_source(kb, "src-does-not-exist")
        assert ei.value.code == "source_not_found"

    def test_remove_cleans_manifest_and_shared_extract_kept(
        self, kb, kb_root, source_dir, tmp_path
    ):
        """지운 원본의 manifest 항목·추출 파일이 사라지고, 공유 추출은 남는다."""
        from hwpx_helper import make_hwpx, p, sec, t

        from wiki_mcp.kb.scanner import scan_sources

        other = tmp_path / "other"
        other.mkdir()
        s1 = add_source(kb, source_dir)["source"]
        s2 = add_source(kb, other)["source"]

        body = p(t("두 원본이 같은 내용을 공유하는 경우의 검증 문단입니다"))
        make_hwpx(source_dir / "a.hwpx", {"Contents/section0.xml": sec(body)})
        make_hwpx(other / "b.hwpx", {"Contents/section0.xml": sec(body)})
        scan_sources(kb, now=future_now())

        extracts_before = list((kb_root / ".kb" / "extract").glob("*.md"))
        assert len(extracts_before) == 1  # dedup — 추출 하나를 공유

        remove_source(kb, s1["id"])
        entries = kb.load_manifest()["entries"]
        assert {e["source_id"] for e in entries} == {s2["id"]}
        assert list((kb_root / ".kb" / "extract").glob("*.md"))  # 공유 추출은 유지

        remove_source(kb, s2["id"])
        assert kb.load_manifest()["entries"] == []
        assert list((kb_root / ".kb" / "extract").glob("*.md")) == []  # 참조가 끊기면 제거


class TestAtomicWrites:
    def test_sources_yaml_roundtrip(self, kb, source_dir):
        add_source(kb, source_dir)
        # 임시 파일이 남지 않는다
        assert [p.name for p in (kb.root).iterdir() if p.name.endswith(".tmp")] == []
        raw = kb.sources_file.read_text(encoding="utf-8")
        assert "sources:" in raw
        assert source_dir.name in raw
