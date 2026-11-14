"""scan_sources — 결정적 스캔·원본 보존 계약.

핵심 보증: 원본 폴더의 파일 목록·내용·해시·mtime 이 스캔 전후로 같고,
같은 상태의 재스캔은 추출을 다시 하지 않는다(extracted 0).
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest
from hwpx_helper import make_hwpx, p, sec, t
from kb_helper import future_now

from wiki_mcp.kb.errors import KBError
from wiki_mcp.kb.scanner import scan_sources
from wiki_mcp.kb.sources import add_source


def _hwpx(path: Path, *paras: str) -> Path:
    return make_hwpx(path, {"Contents/section0.xml": sec("".join(p(t(x)) for x in paras))})


def _snapshot(root: Path) -> dict[str, tuple[str, float, int]]:
    """원본 무결성 스냅샷 — (sha256, mtime, size)."""
    snap: dict[str, tuple[str, float, int]] = {}
    for f in sorted(root.rglob("*")):
        if f.is_file():
            st = f.stat()
            snap[str(f.relative_to(root))] = (
                hashlib.sha256(f.read_bytes()).hexdigest(),
                st.st_mtime,
                st.st_size,
            )
    return snap


@pytest.fixture
def scanned(kb, source_dir):
    """원본 하나(hwp+md)를 등록하고 스캔까지 마친 KB."""
    add_source(kb, source_dir)
    _hwpx(source_dir / "report.hwpx", "출장비 정산 기준을 담은 보고서 본문입니다")
    (source_dir / "memo.md").write_text(
        "# 메모\n\n회의에서 정한 예산 배분 기준에 대한 메모다.", encoding="utf-8"
    )
    scan_sources(kb, now=future_now())
    return kb


class TestOriginalPreservation:
    """원본 보존 시험 — 스캔 두 번, 전후 무결성 동일, 두 번째 추출 0."""

    def test_two_scans_leave_original_untouched(self, kb, source_dir):
        add_source(kb, source_dir)
        sub = source_dir / "2026" / "자료"
        sub.mkdir(parents=True)
        _hwpx(sub / "a.hwpx", "하위 폴더의 한글 문서 본문이 여기 들어간다")
        (source_dir / "b.md").write_text(
            "루트 마크다운 문서 본문 — 충분한 길이를 채운다", encoding="utf-8"
        )
        (source_dir / "c.txt").write_text(
            "텍스트 파일 본문 — 이것도 충분한 길이를 채운다", encoding="utf-8"
        )
        before = _snapshot(source_dir)

        first = scan_sources(kb, now=future_now())
        second = scan_sources(kb, now=future_now())

        assert _snapshot(source_dir) == before
        assert first["new"] == 3 and first["extracted"] == 3
        assert second["new"] == 0 and second["extracted"] == 0
        assert second["unchanged"] == 3

    def test_kb_delete_leaves_no_trace_on_original(self, scanned, source_dir, kb_root):
        """KB 폴더 전체를 지워도 원본은 그대로다(흔적 없음)."""
        before = _snapshot(source_dir)
        import shutil

        shutil.rmtree(kb_root)
        assert _snapshot(source_dir) == before


class TestVerdicts:
    def test_changed_file_reextracted(self, kb, scanned, source_dir):
        (source_dir / "memo.md").write_text(
            "# 메모\n\n바뀐 예산 기준 내용으로 수정했다.", encoding="utf-8"
        )
        out = scan_sources(kb, now=future_now())
        assert out["changed"] == 1
        assert out["extracted"] == 1

    def test_deleted_file_removed_from_manifest(self, kb, scanned, source_dir):
        (source_dir / "memo.md").unlink()
        out = scan_sources(kb, now=future_now())
        assert out["deleted"] == 1
        rels = [e["rel_path"] for e in scanned.load_manifest()["entries"]]
        assert "memo.md" not in rels

    def test_deleted_extract_file_cleaned_when_unreferenced(self, kb, scanned, source_dir):
        target = source_dir / "report.hwpx"
        sha = hashlib.sha256(target.read_bytes()).hexdigest()
        extract = scanned.extract_path(sha)
        assert extract.exists()
        target.unlink()
        scan_sources(kb, now=future_now())
        assert not extract.exists()

    def test_failed_file_retried_and_recovers(self, kb, source_dir):
        """실패(여선 unsupported) 후 형식을 고쳐 다시 넣으면 회복된다."""
        add_source(kb, source_dir)
        bad = source_dir / "note.hwp"
        bad.write_bytes(b"HWP binary placeholder")
        first = scan_sources(kb, now=future_now())
        assert first["failed"] == 1

        bad.unlink()
        (source_dir / "note.md").write_text(
            "회복된 문서 본문입니다. 형식을 고쳐 다시 넣었다.", encoding="utf-8"
        )
        second = scan_sources(kb, now=future_now())
        assert second["deleted"] == 1
        entry = [e for e in kb.load_manifest()["entries"] if e["rel_path"] == "note.md"][0]
        assert entry["status"] == "ok"


class TestExclusions:
    @pytest.mark.parametrize(
        "name",
        [
            "~$report.docx",
            ".~lock.docx#",
            "draft.tmp",
            "backup.bak",
            "Thumbs.db",
            "desktop.ini",
            ".hidden.md",
        ],
    )
    def test_transient_files_ignored(self, kb, source_dir, name):
        add_source(kb, source_dir)
        (source_dir / name).write_text("임시 파일 내용", encoding="utf-8")
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 0 and out["failed"] == 0

    def test_hidden_directory_pruned(self, kb, source_dir):
        add_source(kb, source_dir)
        git_dir = source_dir / ".git"
        git_dir.mkdir()
        (git_dir / "config.md").write_text("숨김 폴더 문서", encoding="utf-8")
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 0

    def test_unknown_extension_ignored_quietly(self, kb, source_dir):
        add_source(kb, source_dir)
        (source_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff")
        (source_dir / "app.exe").write_bytes(b"MZ")
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 0 and out["failed"] == 0

    def test_hwp_recorded_as_unsupported(self, kb, source_dir):
        add_source(kb, source_dir)
        (source_dir / "legacy.hwp").write_bytes(b"HWP binary")
        out = scan_sources(kb, now=future_now())
        assert out["failed"] == 1
        entry = kb.load_manifest()["entries"][0]
        assert entry["status"] == "failed"
        assert entry["reason"] == "unsupported"
        # 두 번째 스캔 — 같은 상태면 조용하다(변경 목록에 중복으로 쌓이지 않는다)
        again = scan_sources(kb, now=future_now())
        assert again["failed"] == 0
        changes = kb.load_last_scan()["changes"]
        assert changes == []

    def test_too_large_recorded(self, kb, source_dir):
        add_source(kb, source_dir)
        big = source_dir / "big.md"
        big.write_text("가" * 100, encoding="utf-8")
        out = scan_sources(kb, now=future_now(), max_file_size=10)
        assert out["failed"] == 1
        entry = kb.load_manifest()["entries"][0]
        assert entry["reason"] == "too_large"

    def test_unstable_file_skipped_then_picked_up(self, kb, source_dir):
        add_source(kb, source_dir)
        f = source_dir / "writing.md"
        f.write_text("저장 중인 문서 내용입니다. 아직 안정화되지 않았다.", encoding="utf-8")
        # now 를 파일 mtime 과 같은 '지금'으로 두면 unstable 로 건너뛴다
        real_now = time.time()
        first = scan_sources(kb, now=lambda: real_now + 0.5)
        assert first["new"] == 0
        # 시간이 흐른 뒤(또는 안정화된 뒤)에는 잡힌다
        second = scan_sources(kb, now=lambda: real_now + 10)
        assert second["new"] == 1

    def test_include_glob_limits_extraction(self, kb, source_dir):
        add_source(kb, source_dir, include=["*.hwpx"])
        _hwpx(source_dir / "a.hwpx", "한글 문서 본문이 여기 들어간다")
        (source_dir / "b.md").write_text("마크다운은 포함 패턴 밖이다", encoding="utf-8")
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 1
        rels = [e["rel_path"] for e in kb.load_manifest()["entries"]]
        assert rels == ["a.hwpx"]

    def test_exclude_glob_skips_matching(self, kb, source_dir):
        add_source(kb, source_dir, exclude=["drafts/*"])
        drafts = source_dir / "drafts"
        drafts.mkdir()
        (drafts / "d.md").write_text("초안 문서 — 이 내용은 충분히 길어야 한다", encoding="utf-8")
        (source_dir / "final.md").write_text(
            "최종 문서 본문 — 이 내용은 충분히 길다", encoding="utf-8"
        )
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 1
        rels = [e["rel_path"] for e in kb.load_manifest()["entries"]]
        assert rels == ["final.md"]

    def test_symlink_escaping_source_skipped(self, kb, source_dir, tmp_path):
        add_source(kb, source_dir)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("원본 밖 문서", encoding="utf-8")
        os.symlink(outside / "secret.md", source_dir / "link.md")
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 0


class TestDedupAndEncodings:
    def test_same_content_shares_one_extract(self, kb, source_dir):
        add_source(kb, source_dir)
        body = p(t("같은 내용을 가진 두 파일의 공유 추출 검증 문단"))
        make_hwpx(source_dir / "one.hwpx", {"Contents/section0.xml": sec(body)})
        make_hwpx(source_dir / "two.hwpx", {"Contents/section0.xml": sec(body)})
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 2
        assert out["extracted"] == 1  # 추출 하나를 공유
        assert len(list(kb.extract_dir.glob("*.md"))) == 1
        assert all(e["status"] == "ok" for e in kb.load_manifest()["entries"])

    def test_cp949_text_file_extracted(self, kb, source_dir):
        add_source(kb, source_dir)
        legacy = source_dir / "legacy.txt"
        legacy.write_bytes("유물 같은 cp949 문서의 본문입니다.".encode("cp949"))
        scan_sources(kb, now=future_now())
        entry = kb.load_manifest()["entries"][0]
        assert entry["status"] == "ok"
        text = kb.extract_path(entry["sha256"]).read_text(encoding="utf-8")
        assert "cp949 문서" in text

    def test_extract_file_format(self, kb, source_dir):
        add_source(kb, source_dir)
        _hwpx(source_dir / "report.hwpx", "추출 파일 형식 검증용 본문 문단")
        scan_sources(kb, now=future_now())
        entry = kb.load_manifest()["entries"][0]
        raw = kb.extract_path(entry["sha256"]).read_text(encoding="utf-8")
        assert raw.startswith("---")
        for field in (
            "source_id:",
            "rel_path:",
            "sha256:",
            "extractor:",
            "extracted_at:",
            "title:",
        ):
            assert field in raw, field
        assert "<!-- section 0 -->" in raw  # 위치 표지
        import frontmatter

        post = frontmatter.loads(raw)
        assert post["rel_path"] == "report.hwpx"
        assert post["extractor"] == "hwpx"
        assert post["title"] == "report"


class TestBookkeeping:
    def test_last_scan_json(self, scanned):
        last = scanned.load_last_scan()
        assert last["scanned_at"]
        assert last["results"]["new"] == 2
        statuses = {c["status"] for c in last["changes"]}
        assert statuses == {"new"}

    def test_manifest_fields(self, scanned, source_dir):
        entries = scanned.load_manifest()["entries"]
        by_rel = {e["rel_path"]: e for e in entries}
        e = by_rel["report.hwpx"]
        assert e["source_id"].startswith("src-")
        assert set(e) >= {
            "source_id",
            "rel_path",
            "size",
            "mtime",
            "sha256",
            "status",
            "reason",
            "extract_id",
            "title",
            "scanned_at",
        }
        assert e["status"] == "ok" and e["reason"] is None
        assert e["size"] == (source_dir / "report.hwpx").stat().st_size

    def test_nfc_normalized_rel_path(self, kb, source_dir):
        """macOS NFD 파일명도 NFC 키 하나로 매니페스트에 남는다."""
        add_source(kb, source_dir)
        import unicodedata

        nfd_name = unicodedata.normalize("NFD", "한글문서.md")
        (source_dir / nfd_name).write_text("NFD 파일명 문서 본문", encoding="utf-8")
        scan_sources(kb, now=future_now())
        rels = [e["rel_path"] for e in kb.load_manifest()["entries"]]
        assert rels == [unicodedata.normalize("NFC", "한글문서.md")]

    def test_lock_prevents_concurrent_scan(self, scanned):
        # 살아 있는 프로세스(테스트 자신)의 최근 잠금 — 진행 중으로 막는다(M6)
        scanned.lock_file.parent.mkdir(parents=True, exist_ok=True)
        scanned.lock_file.write_text(str(os.getpid()), encoding="utf-8")
        past = time.time() - 1
        os.utime(scanned.lock_file, (past, past))
        with pytest.raises(KBError) as ei:
            scan_sources(kb=scanned, now=future_now())
        assert ei.value.code == "scan_in_progress"

    def test_stale_lock_ignored(self, scanned):
        scanned.lock_file.parent.mkdir(parents=True, exist_ok=True)
        scanned.lock_file.write_text("123", encoding="utf-8")
        stale = time.time() - 700  # 10분 넘음
        os.utime(scanned.lock_file, (stale, stale))
        out = scan_sources(kb=scanned, now=future_now())
        assert out["new"] == 0  # 정상 완료
        assert not scanned.lock_file.exists()  # 잠금은 정리됐다

    def test_scan_of_kb_with_no_sources(self, kb):
        out = scan_sources(kb, now=future_now())
        assert out["new"] == 0 and out["extracted"] == 0


# ─── H1: 건너뛰기가 삭제가 되는 문제 ────────────────────────────────
class TestCarryOver:
    """unstable·stat 실패·순회 오류·사라진 원본 폴더는 삭제가 아니다 — 이월한다."""

    def test_unstable_existing_file_carried_over_not_deleted(self, kb, source_dir):
        """수정 직후 재스캔 — deleted 되지 않고 이전 항목이 이월된다(repro A)."""
        add_source(kb, source_dir)
        f = source_dir / "a.md"
        f.write_text("가나다라마바사 충분히 긴 본문 입니다 하하하하", encoding="utf-8")
        scan_sources(kb, now=future_now())
        f.write_text("수정 중인 파일의 새 본문이 충분히 깁니다 정말로", encoding="utf-8")
        r = scan_sources(kb, now=time.time)  # '지금' 기준 — mtime 2초 안
        assert r["deleted"] == 0 and r["new"] == 0 and r["changed"] == 0
        assert r["unchanged"] == 1  # 이월 항목도 지워지지 않았다
        entries = kb.load_manifest()["entries"]
        assert len(entries) == 1
        assert entries[0]["status"] == "ok"
        assert len(list(kb.extract_dir.glob("*.md"))) == 1  # 추출도 유지

    def test_missing_source_root_carries_over_with_warning(self, kb, source_dir, tmp_path):
        """원본 폴더 자체가 없으면 그 원본의 항목은 이월 + source_missing 경고(repro B)."""
        add_source(kb, source_dir)
        (source_dir / "a.md").write_text(
            "가나다라마바사 충분히 긴 본문 입니다 하하하하", encoding="utf-8"
        )
        scan_sources(kb, now=future_now())
        import shutil

        shutil.move(str(source_dir), str(tmp_path / "moved"))
        r = scan_sources(kb, now=future_now())
        assert r["deleted"] == 0
        assert r["warnings"] == [
            {"code": "source_missing", "source_id": kb.load_sources()[0]["id"]}
        ]
        assert len(kb.load_manifest()["entries"]) == 1  # 이월
        assert len(list(kb.extract_dir.glob("*.md"))) == 1  # 추출도 유지

    def test_stat_error_carries_over(self, kb, source_dir, monkeypatch):
        add_source(kb, source_dir)
        f = source_dir / "locked.md"
        f.write_text("통계를 못 얻는 파일의 충분히 긴 본문입니다", encoding="utf-8")
        scan_sources(kb, now=future_now())
        real_stat = Path.stat

        def flaky(self, **kw):
            if self.name == "locked.md":
                raise PermissionError("simulated share violation")
            return real_stat(self, **kw)

        monkeypatch.setattr(Path, "stat", flaky)
        r = scan_sources(kb, now=future_now())
        assert r["deleted"] == 0
        assert r["new"] == 0
        assert len(kb.load_manifest()["entries"]) == 1  # 이월

    def test_walk_error_does_not_delete_subtree(self, kb, source_dir, monkeypatch):
        """하위 폴더 접근 오류가 있어도 그 아래 항목을 삭제하지 않는다."""
        add_source(kb, source_dir)
        sub = source_dir / "2026"
        sub.mkdir()
        (sub / "a.md").write_text("접근 불가 폴더 안의 충분히 긴 본문입니다", encoding="utf-8")
        scan_sources(kb, now=future_now())
        real_scandir = os.scandir

        def blocked(path, *a, **kw):
            if str(path).endswith("2026"):
                raise PermissionError(13, "Permission denied", str(path))
            return real_scandir(path, *a, **kw)

        monkeypatch.setattr(os, "scandir", blocked)
        r = scan_sources(kb, now=future_now())
        assert r["deleted"] == 0
        rels = [e["rel_path"] for e in kb.load_manifest()["entries"]]
        assert "2026/a.md" in rels  # 이월

    def test_mtime_in_future_treated_as_unstable(self, kb, source_dir):
        """살짝 미래 mtime(시계 오차)도 unstable 로 다룬다 — abs(now-mtime) 판정."""
        add_source(kb, source_dir)
        f = source_dir / "future.md"
        f.write_text("미래 시각이 찍힌 파일의 충분히 긴 본문입니다", encoding="utf-8")
        future = time.time() + 0.5  # 살짝 미래 — abs 판정이 아니면 지나쳤을 것
        os.utime(f, (future, future))
        r = scan_sources(kb, now=time.time)
        assert r["new"] == 0  # abs(now - mtime) ≈ 0.5 < 2 — unstable
        assert kb.load_manifest()["entries"] == []
        # 시간이 흐르면(파일이 과거가 되면) 정상 색인된다
        r2 = scan_sources(kb, now=lambda: time.time() + 3)
        assert r2["new"] == 1


# ─── M3: 실패 재시도 정책 ─────────────────────────────────────────────
class TestRetryPolicy:
    def _bad_docx(self, source_dir):
        bad = source_dir / "bad.docx"
        bad.write_bytes(b"not a docx")
        old = time.time() - 60
        os.utime(bad, (old, old))
        return bad

    def test_permanent_failure_not_retried_or_recounted(self, kb, source_dir):
        """corrupt 실패는 size/mtime이 같으면 재시도·재카운트하지 않는다(repro F)."""
        add_source(kb, source_dir)
        self._bad_docx(source_dir)
        first = scan_sources(kb, now=future_now())
        assert first["failed"] == 1
        second = scan_sources(kb, now=future_now())
        assert second["failed"] == 0
        assert kb.load_last_scan()["changes"] == []
        # 상태에는 남는다
        entries = kb.load_manifest()["entries"]
        assert [e["reason"] for e in entries] == ["corrupt"]

    def test_transient_failure_retried_every_scan(self, kb, source_dir, monkeypatch):
        """locked 실패는 매 스캔 재시도한다 — 잠금이 풀리면 회복된다."""
        add_source(kb, source_dir)
        f = source_dir / "busy.md"
        f.write_text("잠긴 파일의 충분히 긴 본문입니다", encoding="utf-8")

        from wiki_mcp.kb import scanner as scanner_mod

        orig_sha = scanner_mod._sha256_file

        def locked_sha(path):
            if path.name == "busy.md":
                raise PermissionError("simulated lock")
            return orig_sha(path)

        monkeypatch.setattr(scanner_mod, "_sha256_file", locked_sha)
        first = scan_sources(kb, now=future_now())
        assert first["failed"] == 1
        monkeypatch.setattr(scanner_mod, "_sha256_file", orig_sha)
        second = scan_sources(kb, now=future_now())
        assert second["failed"] == 0
        assert second["changed"] == 1  # 잠금 풀림 — 실패 항목을 다시 뽑아 성공


# ─── M4: unchanged 갱신·고아 추출 정리 ───────────────────────────────
class TestBookkeepingUpdates:
    def test_unchanged_updates_size_and_mtime(self, kb, source_dir):
        add_source(kb, source_dir)
        f = source_dir / "memo.md"
        f.write_text("내용이 같은 문서의 충분히 긴 본문입니다", encoding="utf-8")
        scan_sources(kb, now=future_now())
        # 같은 내용, 다른 mtime — unchanged 로 이월되되 size/mtime은 새 값으로
        new_mtime = time.time() - 5000
        os.utime(f, (new_mtime, new_mtime))
        scan_sources(kb, now=future_now())
        e = kb.load_manifest()["entries"][0]
        assert e["mtime"] == new_mtime

    def test_changed_orphan_extract_cleaned(self, kb, source_dir):
        """내용이 바뀌면 참조가 끊긴 옛 추출 파일도 정리한다(repro D)."""
        add_source(kb, source_dir)
        f = source_dir / "a.md"
        f.write_text("버전 하나의 충분히 긴 본문 입니다 하하하하하", encoding="utf-8")
        scan_sources(kb, now=future_now())
        assert len(list(kb.extract_dir.glob("*.md"))) == 1
        time.sleep(0.01)
        f.write_text("버전 둘의 충분히 긴 다른 본문 입니다 하하하하하하", encoding="utf-8")
        old = time.time() - 100
        os.utime(f, (old, old))
        r = scan_sources(kb, now=future_now())
        assert r["changed"] == 1
        assert len(list(kb.extract_dir.glob("*.md"))) == 1  # 옛 것은 지워지고 새 것만

    def test_remove_source_orphan_extract_cleaned(self, kb, source_dir):
        from wiki_mcp.kb.sources import remove_source

        add_source(kb, source_dir)
        (source_dir / "a.md").write_text("제거될 원본의 충분히 긴 본문입니다", encoding="utf-8")
        scan_sources(kb, now=future_now())
        assert len(list(kb.extract_dir.glob("*.md"))) == 1
        remove_source(kb, kb.load_sources()[0]["id"])
        assert len(list(kb.extract_dir.glob("*.md"))) == 0


# ─── M9: manifest 손상 복구 ───────────────────────────────────────────
class TestCorruptManifestRecovery:
    def test_scan_quarantines_corrupt_manifest_and_recovers(self, kb, source_dir):
        add_source(kb, source_dir)
        (source_dir / "a.md").write_text("복구 검증용 충분히 긴 본문입니다", encoding="utf-8")
        scan_sources(kb, now=future_now())
        kb.manifest_file.write_text("{ not json", encoding="utf-8")
        r = scan_sources(kb, now=future_now())
        assert r["new"] == 1  # 빈 상태에서 다시 색인
        quarantined = list(kb.meta_dir.glob("manifest.json.corrupt-*"))
        assert len(quarantined) == 1
        entries = kb.load_manifest()["entries"]
        assert [e["status"] for e in entries] == ["ok"]

    def test_other_commands_still_raise_corrupt_manifest(self, kb, source_dir):
        add_source(kb, source_dir)
        kb.meta_dir.mkdir(parents=True, exist_ok=True)
        kb.manifest_file.write_text("][", encoding="utf-8")
        from wiki_mcp.kb.status import kb_status

        with pytest.raises(KBError) as ei:
            kb_status(kb)
        assert ei.value.code == "corrupt_manifest"


# ─── M12: 민감 파일·폴더 건너뛰기 ────────────────────────────────────
class TestSensitiveFiles:
    def test_sensitive_files_skipped_quietly(self, kb, source_dir):
        add_source(kb, source_dir)
        (source_dir / "normal.md").write_text("정상 문서의 충분히 긴 본문입니다", encoding="utf-8")
        (source_dir / "passwords.md").write_text("패스워드 목록이 담긴 문서", encoding="utf-8")
        (source_dir / "server.pem").write_text("-----BEGIN KEY-----", encoding="utf-8")
        (source_dir / "backup.env").write_text("SECRET=1", encoding="utf-8")
        (source_dir / "비밀번호_메모.txt").write_text("비밀번호: 1234", encoding="utf-8")
        r = scan_sources(kb, now=future_now())
        assert r["new"] == 1
        assert r["skipped_sensitive"] == 4
        rels = [e["rel_path"] for e in kb.load_manifest()["entries"]]
        assert rels == ["normal.md"]

    def test_sensitive_folder_pruned_and_carried_over(self, kb, source_dir):
        add_source(kb, source_dir)
        secrets = source_dir / "credentials"
        secrets.mkdir()
        (secrets / "c.md").write_text("민감 폴더 안 문서의 본문", encoding="utf-8")
        r = scan_sources(kb, now=future_now())
        assert r["new"] == 0
        assert kb.load_manifest()["entries"] == []
        # 폴더가 사라져도 deleted 가 아니다(애초에 색인 안 함)
        r2 = scan_sources(kb, now=future_now())
        assert r2["deleted"] == 0


# ─── M14: 해시·추출 TOCTOU ───────────────────────────────────────────
class TestTOCTOU:
    def test_file_changed_during_extraction_is_unstable(self, kb, source_dir, monkeypatch):
        from wiki_mcp.kb import scanner as scanner_mod

        add_source(kb, source_dir)
        f = source_dir / "race.md"
        f.write_text("경합 전 내용의 충분히 긴 본문입니다", encoding="utf-8")
        scan_sources(kb, now=future_now())  # 첫 색인
        f.write_text("경합 중 바뀐 내용의 충분히 긴 본문입니다", encoding="utf-8")
        old = time.time() - 100
        os.utime(f, (old, old))

        real_extract = scanner_mod._extract_entry_text

        def racing_extract(path, **kw):
            result = real_extract(path, **kw)
            if path.name == "race.md":  # 추출 직후 파일이 또 바뀐다
                f.write_text("추출 뒤 또 바뀐 내용의 충분히 긴 본문", encoding="utf-8")
                new_time = time.time() + 10
                os.utime(f, (new_time, new_time))
            return result

        monkeypatch.setattr(scanner_mod, "_extract_entry_text", racing_extract)
        r = scan_sources(kb, now=future_now())
        assert r["changed"] == 0 and r["new"] == 0  # unstable — 반영하지 않는다
        entries = kb.load_manifest()["entries"]
        assert len(entries) == 1
        assert entries[0]["status"] == "ok"  # 이전 항목 이월


# ─── M6: 스캔 잠금 — PID 생존 확인 ───────────────────────────────────
class TestScanLockPid:
    def _write_lock(self, kb, pid: int):
        kb.lock_file.parent.mkdir(parents=True, exist_ok=True)
        kb.lock_file.write_text(str(pid), encoding="utf-8")
        past = time.time() - 1
        os.utime(kb.lock_file, (past, past))

    def test_live_foreign_lock_blocks_scan(self, scanned):
        """살아 있는 다른 프로세스의 잠금은 나이가 찼어도(아직 10분 안) 막는다."""
        self._write_lock(scanned, os.getpid())  # 테스트 프로세스 = 살아 있음
        with pytest.raises(KBError) as ei:
            scan_sources(kb=scanned, now=future_now())
        assert ei.value.code == "scan_in_progress"

    def test_dead_pid_lock_ignored(self, scanned):
        """죽은 PID의 잠금은 최근 것이어도 무시한다(경합 정리)."""
        self._write_lock(scanned, 999999998)  # 존재할 수 없는 PID
        out = scan_sources(kb=scanned, now=future_now())
        assert out["new"] == 0
        assert not scanned.lock_file.exists()


# ─── S3: 순회 가지치기·파일 resolve ──────────────────────────────────
class TestWalkGuards:
    def test_junction_to_outside_pruned(self, kb, source_dir, tmp_path):
        """원본 밖을 가리키는 폴더 심볼릭 링크(Windows 정션 근사)는 가지치기."""
        add_source(kb, source_dir)
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        (outside / "x.md").write_text("원본 밖 폴더의 문서", encoding="utf-8")
        os.symlink(outside, source_dir / "junction")
        r = scan_sources(kb, now=future_now())
        assert r["new"] == 0

    def test_windows_hidden_or_reparse_dir_pruned(self, kb, source_dir, monkeypatch):
        """Windows 숨김·시스템·reparse 속성 폴더는 stat 결과만으로 가지치기한다."""
        add_source(kb, source_dir)
        sysdir = source_dir / "sysdir"
        sysdir.mkdir()
        (sysdir / "s.md").write_text("시스템 속성 폴더의 문서", encoding="utf-8")
        real_stat = Path.stat

        class _WinStat:
            """st_file_attributes 를 덧입힌 stat 위임 객체(Windows 근사)."""

            def __init__(self, real, attrs):
                self._real = real
                self.st_file_attributes = attrs

            def __getattr__(self, name):
                return getattr(self._real, name)

        def fake_stat(self, **kw):
            st = real_stat(self, **kw)
            if "sysdir" in str(self):
                return _WinStat(st, 0x2 | 0x400)  # hidden + reparse(정션)
            return st

        monkeypatch.setattr(Path, "stat", fake_stat)
        r = scan_sources(kb, now=future_now())
        assert r["new"] == 0


# ─── H7: 스캔 중 등록 해제 — 저장 직전 재확인 ────────────────────────
class TestConcurrentRemove:
    def test_source_removed_during_scan_is_dropped_on_save(self, kb, source_dir, monkeypatch):
        """스캔 도중 원본이 해제되면 그 항목은 저장에서 버린다(유령 조각 방지)."""
        from wiki_mcp.kb import scanner as scanner_mod
        from wiki_mcp.kb.sources import remove_source

        add_source(kb, source_dir)
        (source_dir / "a.md").write_text("동시성 검증용 충분히 긴 본문입니다", encoding="utf-8")
        real_process = scanner_mod._process_file
        called = []

        def racing_process(*args, **kw):
            result = real_process(*args, **kw)
            if not called:
                called.append(True)
                remove_source(kb, kb.load_sources()[0]["id"])  # 스캔 중 해제
            return result

        monkeypatch.setattr(scanner_mod, "_process_file", racing_process)
        r = scan_sources(kb, now=future_now())
        assert r["new"] == 1
        entries = kb.load_manifest()["entries"]
        assert entries == []  # 저장 직전 sources 재로드로 버림
        assert list(kb.extract_dir.glob("*.md")) == []  # 고아 추출도 정리
