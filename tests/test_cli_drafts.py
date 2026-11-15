"""CLI 하위 명령 drafts-*·mark-orphans (설계 3.7절, C1~C8).

서브프로세스로 실제 진입점을 실행해 JSON 1줄·종료 코드·UTF-8·1MB 상한을
검증한다. 원본 mtime 은 스캔 전 10초 이상 과거로 돌린다(UNSTABLE_SECONDS=2).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from conftest import ensure_subprocess_tmp_ok
from test_cli import parse_stdout, run_cli

QUOTE = "국내 출장 일비는 하루 2만 원으로 한다"


@pytest.fixture(autouse=True)
def _subprocess_tmp_guard(tmp_path: Path):
    ensure_subprocess_tmp_ok(tmp_path)


@pytest.fixture
def kb_dir(tmp_path: Path) -> str:
    return str(tmp_path / "지식기반")


def age_file(p: Path, seconds: float = 30.0) -> None:
    old = time.time() - seconds
    os.utime(p, (old, old))


def setup_kb(kb_dir: str, tmp_path: Path, *, text: str | None = None) -> dict:
    """init → add-source → scan 까지. manifest 첫 항목을 돌려준다."""
    docs = tmp_path / "원본 자료"
    docs.mkdir(exist_ok=True)
    f = docs / "규정.md"
    f.write_text(text if text is not None else f"# 출장 규정\n\n{QUOTE}.\n", encoding="utf-8")
    age_file(f)
    assert run_cli(["kb", "init", "--kb", kb_dir]).returncode == 0
    assert run_cli(["kb", "add-source", "--kb", kb_dir, str(docs)]).returncode == 0
    r = run_cli(["kb", "scan", "--kb", kb_dir])
    assert r.returncode == 0, r.stderr
    entry = parse_stdout(r)["changes"][0]
    return {"source_id": entry["source_id"], "rel_path": entry["rel_path"], "quote": QUOTE}


def write_draft_input(
    tmp_path: Path, entry: dict, *, note_id: str = "travel-expense-rule", body: str = "본문이다."
) -> Path:
    payload = {
        "kind": "new",
        "note_id": note_id,
        "title": "출장비 기준",
        "tags": ["출장"],
        "body": body,
        "evidence": [
            {
                "source_id": entry["source_id"],
                "rel_path": entry["rel_path"],
                "quote": entry["quote"],
            }
        ],
    }
    p = tmp_path / "draft-input.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def first_draft_id(kb_dir: str) -> str:
    out = parse_stdout(run_cli(["kb", "drafts-list", "--kb", kb_dir]))
    return out["drafts"][0]["id"]


class TestDraftsCommands:
    def test_c1_seven_commands_json_and_exit_zero(self, kb_dir, tmp_path):
        entry = setup_kb(kb_dir, tmp_path)
        # 반려용 초안 1건 + 승인용 초안 1건
        for nid in ("reject-note", "approve-note"):
            p = write_draft_input(tmp_path, entry, note_id=nid)
            r = run_cli(["kb", "drafts-save", "--kb", kb_dir, "--input", str(p)])
            assert r.returncode == 0, r.stderr
            parse_stdout(r)  # JSON 정확히 1줄
        # drafts-reject
        r = run_cli(["kb", "drafts-reject", "--kb", kb_dir, first_draft_id(kb_dir)])
        assert r.returncode == 0
        parse_stdout(r)
        # drafts-list
        r = run_cli(["kb", "drafts-list", "--kb", kb_dir])
        assert r.returncode == 0
        assert parse_stdout(r)["total"] == 1
        # drafts-show
        r = run_cli(["kb", "drafts-show", "--kb", kb_dir, first_draft_id(kb_dir)])
        assert r.returncode == 0
        parse_stdout(r)
        # drafts-approve
        r = run_cli(["kb", "drafts-approve", "--kb", kb_dir, first_draft_id(kb_dir)])
        assert r.returncode == 0
        approve_out = parse_stdout(r)
        assert approve_out["path"] == "concepts/approve-note.md"
        # drafts-history
        r = run_cli(["kb", "drafts-history", "--kb", kb_dir])
        assert r.returncode == 0
        assert parse_stdout(r)["total"] == 2
        # mark-orphans
        r = run_cli(["kb", "mark-orphans", "--kb", kb_dir])
        assert r.returncode == 0
        parse_stdout(r)
        # stdout UTF-8 — 한글이 깨지지 않고 파싱됐다(위에서 확인)

    def test_c2_error_codes(self, kb_dir, tmp_path):
        entry = setup_kb(kb_dir, tmp_path)
        p = write_draft_input(tmp_path, entry)
        run_cli(["kb", "drafts-save", "--kb", kb_dir, "--input", str(p)])
        r = run_cli(["kb", "drafts-show", "--kb", kb_dir, "../x"])
        assert r.returncode == 1
        assert parse_stdout(r)["error"]["code"] == "invalid_draft_id"
        r = run_cli(["kb", "drafts-show", "--kb", kb_dir, "d-20261115-090000-abcdef"])
        assert r.returncode == 1
        assert parse_stdout(r)["error"]["code"] == "draft_not_found"
        r = run_cli(["kb", "drafts-list", "--kb", kb_dir, "--limit", "abc"])
        assert r.returncode == 1
        assert parse_stdout(r)["error"]["code"] == "invalid_arguments"

    def test_c3_save_then_list_and_show_body(self, kb_dir, tmp_path):
        entry = setup_kb(kb_dir, tmp_path)
        body = "초안 본문 — [[다른-노트]] 링크 포함."
        p = write_draft_input(tmp_path, entry, body=body)
        r = run_cli(["kb", "drafts-save", "--kb", kb_dir, "--input", str(p)])
        assert r.returncode == 0, r.stderr
        out = parse_stdout(run_cli(["kb", "drafts-list", "--kb", kb_dir]))
        assert out["total"] == 1
        shown = parse_stdout(run_cli(["kb", "drafts-show", "--kb", kb_dir, out["drafts"][0]["id"]]))
        assert shown["draft"]["body"] == body

    def test_c4_approve_creates_note_and_history(self, kb_dir, tmp_path):
        entry = setup_kb(kb_dir, tmp_path)
        p = write_draft_input(tmp_path, entry)
        run_cli(["kb", "drafts-save", "--kb", kb_dir, "--input", str(p)])
        draft_id = first_draft_id(kb_dir)
        r = run_cli(["kb", "drafts-approve", "--kb", kb_dir, draft_id])
        assert r.returncode == 0, r.stderr
        note = Path(kb_dir) / "wiki" / "concepts" / "travel-expense-rule.md"
        assert note.is_file()
        hist = parse_stdout(run_cli(["kb", "drafts-history", "--kb", kb_dir]))
        assert hist["entries"][0]["actor"] == "cli"
        assert hist["entries"][0]["action"] == "approved"

    def test_c5_worst_case_show_under_1mb(self, kb_dir, tmp_path):
        entry = setup_kb(kb_dir, tmp_path)
        # update 대상 노트 — 본문 100,000자 이상
        note_file = Path(kb_dir) / "wiki" / "concepts" / "big-note.md"
        note_file.parent.mkdir(parents=True, exist_ok=True)
        note_file.write_text(
            '---\nid: big-note\ntitle: "큰 노트"\n---\n\n' + "나" * 120000,
            encoding="utf-8",
        )
        # 최악 크기 초안 — 한글 본문 20,000자, 근거 10개, 각 인용 500자, context 2000
        blocks = [f"구간{i:02d} " + "가" * 5998 for i in range(10)]
        big = tmp_path / "원본 자료" / "대용량.md"
        big.write_text("# 대용량\n\n" + "\n\n".join(blocks) + "\n", encoding="utf-8")
        age_file(big)
        scan = run_cli(["kb", "scan", "--kb", kb_dir])
        assert scan.returncode == 0, scan.stderr
        assert parse_stdout(scan)["new"] == 1  # 대용량.md 스캔 성공
        payload = {
            "kind": "update",
            "note_id": "big-note",
            "body": "다" * 20000,
            "evidence": [
                {
                    "source_id": entry["source_id"],
                    "rel_path": "대용량.md",
                    "quote": f"구간{i:02d} " + "가" * 495,
                }
                for i in range(10)
            ],
        }
        p = tmp_path / "big-draft.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        r = run_cli(["kb", "drafts-save", "--kb", kb_dir, "--input", str(p)])
        assert r.returncode == 0, r.stderr + r.stdout
        r = run_cli(
            ["kb", "drafts-show", "--kb", kb_dir, first_draft_id(kb_dir), "--context", "2000"]
        )
        assert r.returncode == 0, r.stderr
        assert len(r.stdout.encode("utf-8")) < 1_048_576

    def test_c6_reject_reason_leading_dash(self, kb_dir, tmp_path):
        entry = setup_kb(kb_dir, tmp_path)
        p = write_draft_input(tmp_path, entry)
        run_cli(["kb", "drafts-save", "--kb", kb_dir, "--input", str(p)])
        draft_id = first_draft_id(kb_dir)
        r = run_cli(["kb", "drafts-reject", "--kb", kb_dir, draft_id, "--reason=-앞에 대시"])
        assert r.returncode == 0, r.stderr
        hist = parse_stdout(run_cli(["kb", "drafts-history", "--kb", kb_dir]))
        assert hist["entries"][0]["reason"] == "-앞에 대시"

    def test_c7_scan_has_orphans_and_legacy_fields(self, kb_dir, tmp_path):
        setup_kb(kb_dir, tmp_path)
        out = parse_stdout(run_cli(["kb", "scan", "--kb", kb_dir]))
        for key in (
            "new",
            "changed",
            "deleted",
            "unchanged",
            "failed",
            "extracted",
            "skipped_sensitive",
            "duration_ms",
            "changes",
            "warnings",
        ):
            assert key in out, key
        assert "orphans" in out
        assert out["orphans"] is not None
        assert out["orphans"]["checked"] == 0

    def test_c8_korean_path_with_space(self, tmp_path):
        kb_dir = str(tmp_path / "내 지식 기반")
        entry = setup_kb(kb_dir, tmp_path)
        p = write_draft_input(tmp_path, entry)
        assert run_cli(["kb", "drafts-save", "--kb", kb_dir, "--input", str(p)]).returncode == 0
        assert parse_stdout(run_cli(["kb", "drafts-list", "--kb", kb_dir]))["total"] == 1
        assert (
            run_cli(["kb", "drafts-approve", "--kb", kb_dir, first_draft_id(kb_dir)]).returncode
            == 0
        )
        hist = parse_stdout(run_cli(["kb", "drafts-history", "--kb", kb_dir]))
        assert hist["entries"][0]["action"] == "approved"
        assert (Path(kb_dir) / "wiki" / "concepts" / "travel-expense-rule.md").is_file()

    def test_drafts_save_input_size_limit(self, kb_dir, tmp_path):
        setup_kb(kb_dir, tmp_path)
        p = tmp_path / "too-big.json"
        p.write_text("x" * (1_048_576 + 1), encoding="utf-8")
        r = run_cli(["kb", "drafts-save", "--kb", kb_dir, "--input", str(p)])
        assert r.returncode == 1
        assert parse_stdout(r)["error"]["code"] == "draft_too_large"

    def test_drafts_save_broken_json_is_invalid_draft(self, kb_dir, tmp_path):
        setup_kb(kb_dir, tmp_path)
        p = tmp_path / "broken.json"
        p.write_text("{ 깨진", encoding="utf-8")
        r = run_cli(["kb", "drafts-save", "--kb", kb_dir, "--input", str(p)])
        assert r.returncode == 1
        assert parse_stdout(r)["error"]["code"] == "invalid_draft"


# ─── 0.4.0 리뷰 반영 — 대기열 선점 해소(보안 7) ────────────────────────
class TestRejectAllStale:
    def test_reject_all_stale_only_stale_drafts(self, kb_dir, tmp_path):
        """drafts-reject --all-stale — draft_stale 초안만 일괄 반려한다."""
        docs = tmp_path / "원본 자료"
        docs.mkdir(exist_ok=True)
        f1 = docs / "규정.md"
        f1.write_text(f"# 출장 규정\n\n{QUOTE}.\n", encoding="utf-8")
        f2 = docs / "식대.md"
        f2.write_text(f"# 식대 규정\n\n{QUOTE} — 식대 편.\n", encoding="utf-8")
        age_file(f1)
        age_file(f2)
        assert run_cli(["kb", "init", "--kb", kb_dir]).returncode == 0
        assert run_cli(["kb", "add-source", "--kb", kb_dir, str(docs)]).returncode == 0
        scan = run_cli(["kb", "scan", "--kb", kb_dir])
        assert scan.returncode == 0, scan.stderr
        entries = parse_stdout(scan)["changes"]
        e1 = next(e for e in entries if e["rel_path"] == "규정.md")
        e2 = next(e for e in entries if e["rel_path"] == "식대.md")

        def draft_file(nid: str, entry: dict) -> Path:
            payload = {
                "kind": "new",
                "note_id": nid,
                "title": "제목",
                "tags": ["태그"],
                "body": "본문이다.",
                "evidence": [
                    {
                        "source_id": entry["source_id"],
                        "rel_path": entry["rel_path"],
                        "quote": QUOTE,
                    }
                ],
            }
            p = tmp_path / f"draft-{nid}.json"
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return p

        for nid, entry in (("stale-note", e1), ("fresh-note", e2)):
            p = draft_file(nid, entry)
            assert run_cli(["kb", "drafts-save", "--kb", kb_dir, "--input", str(p)]).returncode == 0

        f1.write_text(f"# 출장 규정\n\n{QUOTE} — 개정되었다.\n", encoding="utf-8")
        age_file(f1)
        assert run_cli(["kb", "scan", "--kb", kb_dir]).returncode == 0

        r = run_cli(["kb", "drafts-reject", "--kb", kb_dir, "--all-stale", "--reason=원본 변경"])
        assert r.returncode == 0, r.stderr
        out = parse_stdout(r)
        assert len(out["rejected"]) == 1
        assert len(out["history_seqs"]) == 1
        assert out["rejected"][0].startswith("d-")
        # fresh 초안은 남아 있다
        listed = parse_stdout(run_cli(["kb", "drafts-list", "--kb", kb_dir]))
        assert listed["total"] == 1
        assert listed["drafts"][0]["note_id"] == "fresh-note"
        hist = parse_stdout(run_cli(["kb", "drafts-history", "--kb", kb_dir]))
        assert hist["entries"][0]["reason"] == "원본 변경"
        assert hist["entries"][0]["action"] == "rejected"

    def test_all_stale_with_nothing_stale_is_empty_ok(self, kb_dir, tmp_path):
        entry = setup_kb(kb_dir, tmp_path)
        p = write_draft_input(tmp_path, entry)
        assert run_cli(["kb", "drafts-save", "--kb", kb_dir, "--input", str(p)]).returncode == 0
        r = run_cli(["kb", "drafts-reject", "--kb", kb_dir, "--all-stale"])
        assert r.returncode == 0
        out = parse_stdout(r)
        assert out == {"rejected": [], "history_seqs": []}
        assert parse_stdout(run_cli(["kb", "drafts-list", "--kb", kb_dir]))["total"] == 1

    def test_all_stale_conflicts_with_positional_id(self, kb_dir, tmp_path):
        setup_kb(kb_dir, tmp_path)
        r = run_cli(
            ["kb", "drafts-reject", "--kb", kb_dir, "--all-stale", "d-20261115-120000-aaaaaa"]
        )
        assert r.returncode == 1
        assert parse_stdout(r)["error"]["code"] == "invalid_arguments"
