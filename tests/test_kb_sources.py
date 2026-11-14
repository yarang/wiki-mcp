"""KB 원본 관리 — add_source / list_sources / remove_source 계약.

경로 검증(거부 규칙), 민감 이름 경고, 원본 무결성(remove 가 원본을 건드리지
않는지)을 검증한다. 홈·시스템 폴더 판정은 home 경로를 주입해 결정적으로
검증한다(테스트에 시스템 절대 경로를 하드코딩하지 않는다).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from kb_helper import future_now

from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.sources import add_source, list_sources, remove_source
from wiki_mcp.kb.store import KnowledgeBase


class TestRemotePaths:
    """UNC·장치 네임스페이스 경로는 파일시스템을 건드리기 전에 거부한다(S4).

    SMB 서버에 자격증명(NTLM)이 새는 것을 막는다 — exists()·resolve() 조차
    네트워크 왕복을 일으킬 수 있어 문자열 단계에서 끊는다.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "\\\\server\\share\\docs",
            "//server/share/docs",
            "\\\\?\\C:\\Users",
            "\\\\.\\PhysicalDrive0",
        ],
    )
    def test_remote_paths_rejected_before_fs(self, kb, path):
        with pytest.raises(KBError) as ei:
            add_source(kb, path)
        assert ei.value.code == "source_remote_path"

    def test_sources_yaml_remote_path_rejected(self, kb):
        kb.save_sources([{"id": "src-x", "path": "\\\\nas\\share", "include": [], "exclude": []}])
        with pytest.raises(KBError) as ei:
            kb.load_sources()
        assert ei.value.code == "source_remote_path"


class TestForbiddenRoots:
    """금지 폴더 규칙 — conftest가 기본 목록을 꺼도 원본 규칙은 그대로 검증한다.

    Windows CI(windows-latest)의 tmp_path는 %LOCALAPPDATA%\\Temp 안이라 기본
    AppData 규칙에 걸린다. 그래서 테스트 중에는 금지 목록 팩토리를 끄고
    conftest 가 아닌 이 테스트에서만 원본 계산으로 판정한다.
    """

    def _forbidden(self, home: Path, p: Path) -> bool:
        from wiki_mcp.kb.sources import _default_forbidden_roots
        from wiki_mcp.kb.store import is_same_or_inside

        target = Path(p).resolve()
        if target == target.parent:
            return True
        if is_same_or_inside(home, target):  # 홈 자신·홈의 모든 상위 폴더
            return True
        return any(is_same_or_inside(target, c) for c in _default_forbidden_roots(home))

    def test_appdata_rejected_even_without_env_vars(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        assert self._forbidden(home, home / "AppData")
        assert self._forbidden(home, home / "AppData" / "Local" / "Temp" / "pytest-1")
        assert self._forbidden(home, home / "Library" / "Mobile Documents")

    def test_home_ancestors_rejected(self, tmp_path):
        home = tmp_path / "home" / "user"
        home.mkdir(parents=True)
        assert self._forbidden(home, home.parent)  # C:\\Users, /Users, /home 에 해당
        assert self._forbidden(home, home)
        # 홈 안의 일반 폴더는 거부 아님
        assert not self._forbidden(home, home / "Documents")

    def test_posix_mount_roots_rejected(self, tmp_path):
        home = tmp_path / "home" / "user"
        home.mkdir(parents=True)
        for name in ("/Volumes", "/mnt", "/media"):
            assert self._forbidden(home, Path(name) / "usb"), name

    def test_windows_system_roots_from_env(self, tmp_path, monkeypatch):
        """SystemRoot 등 후보는 C: 고정 문자열이 아니라 환경 변수로 만든다."""
        home = tmp_path / "home"
        home.mkdir()
        win = tmp_path / "win-root"
        win.mkdir()
        monkeypatch.setenv("SystemRoot", str(win))
        from wiki_mcp.kb.sources import _default_forbidden_roots

        roots = [str(r) for r in _default_forbidden_roots(home)]
        assert str(win) in roots

    def test_injected_empty_roots_allow_tmp_paths(self, kb, tmp_path):
        """conftest 가 끄는 팩토리가 꺼져 있으면 tmp_path 등록이 막히지 않는다.

        이 테스트 자체가 autouse fixture(팩토리 → [])를 거친다 — Windows CI의
        AppData Temp 경로 tmp_path가 system_folder 없이 등록되어야 한다.
        """
        somewhere = tmp_path / "somewhere"
        somewhere.mkdir()
        out = add_source(kb, somewhere)
        assert "source" in out


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

    def test_system_folder_rejected_with_injected_home(self, kb, tmp_path, monkeypatch):
        """home을 주입하면 ~/Library·~/AppData가 그대로 거부된다(팩토리 복원 검증).

        conftest 가 기본 팩토리를 꺼 두었으니 이 테스트만 원본 팩토리로 되돌려
        add_source 전체 경로(오류 코드 system_folder 포함)를 확인한다.
        """
        from wiki_mcp.kb import sources as kb_sources

        monkeypatch.setattr(
            kb_sources, "_forbidden_roots_factory", kb_sources._default_forbidden_roots
        )
        for name in ("Library", "AppData"):
            sysdir = tmp_path / name
            sysdir.mkdir()
            with pytest.raises(KBError) as ei:
                add_source(kb, sysdir, home=tmp_path)
            assert ei.value.code == "system_folder"

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


# ─── H6: 원본 id 중복 방지 ────────────────────────────────────────────
class TestSourceIds:
    def test_removed_id_not_reused(self, kb, tmp_path):
        """삭제 후 재등록 — 기존 최대 번호 + 1 을 쓴다(중복 없음, repro C)."""
        dirs = []
        for n in range(3):
            d = tmp_path / f"s{n}"
            d.mkdir()
            dirs.append(d)
        ids = [add_source(kb, d)["source"]["id"] for d in dirs]
        removed = ids[0]
        remove_source(kb, removed)
        again = add_source(kb, dirs[0])["source"]["id"]
        assert again not in ids[1:]
        assert again != removed
        all_ids = [s["id"] for s in list_sources(kb)["sources"]]
        assert len(all_ids) == len(set(all_ids))  # 중복 없음

    def test_duplicate_ids_in_yaml_rejected(self, kb, source_dir):
        kb.save_sources(
            [
                {"id": "src-1", "path": str(source_dir), "include": [], "exclude": []},
                {"id": "src-1", "path": str(source_dir) + "2", "include": [], "exclude": []},
            ]
        )
        with pytest.raises(KBError) as ei:
            kb.load_sources()
        assert ei.value.code == "corrupt_sources"


# ─── M8: sources.yaml 형식 검증 ───────────────────────────────────────
class TestSourcesValidation:
    def _write(self, kb, raw: str):
        kb.sources_file.parent.mkdir(parents=True, exist_ok=True)
        kb.sources_file.write_text(raw, encoding="utf-8")

    @pytest.mark.parametrize(
        "raw",
        [
            "- src-1\n- src-2\n",  # 최상위가 리스트
            "sources: 'oops'\n",  # sources가 문자열
            "sources:\n  - 평범한 문자열 항목\n",  # 항목이 문자열
            "sources:\n  - id: src-1\n    include: []\n    exclude: []\n",  # path 없음
            "sources:\n  - id: src-1\n    path: /tmp/x\n    include: '*.hwpx'\n    exclude: []\n",  # include가 문자열
        ],
    )
    def test_malformed_yaml_is_corrupt_sources(self, kb, raw):
        self._write(kb, raw)
        with pytest.raises(KBError) as ei:
            kb.load_sources()
        assert ei.value.code == "corrupt_sources"

    def test_valid_entries_load(self, kb, source_dir):
        kb.save_sources(
            [
                {
                    "id": "src-1",
                    "path": str(source_dir),
                    "include": ["*.hwpx"],
                    "exclude": ["drafts/*"],
                    "added_at": "2026-01-01T00:00:00+00:00",
                }
            ]
        )
        sources = kb.load_sources()
        assert sources[0]["id"] == "src-1"
        assert sources[0]["include"] == ["*.hwpx"]

    def test_corrupt_sources_is_input_error_via_cli(self, tmp_path):
        import json
        import subprocess
        import sys
        from pathlib import Path

        kb_dir = tmp_path / "kb"
        kb_dir.mkdir(parents=True, exist_ok=True)
        (kb_dir / "sources.yaml").write_text("- a\n- b\n", encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-m", "wiki_mcp", "kb", "list-sources", "--kb", str(kb_dir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
            },
            timeout=60,
        )
        assert run.returncode == 1  # 종료 코드 1(cli-contract)
        assert json.loads(run.stdout.strip())["error"]["code"] == "corrupt_sources"
