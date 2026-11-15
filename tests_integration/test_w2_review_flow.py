"""W2 통합 인수 조건 — CLI ↔ MCP stdio ↔ 검토 대기열 흐름 (I1~I4).

행 디렉터리 밖의 종단 테스트. `uv run --extra dev pytest tests_integration -q`
로 실행한다(pyproject testpaths 밖이라 기본 pytest 에는 포함되지 않는다).
roadmap C4 완료 기준 "초안 3건 승인 → 출처 붙은 노트 3개"를 재현한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import frontmatter
import pytest
from conftest import ensure_subprocess_tmp_ok
from test_kb_server import McpStdioClient, _base_env, _tools_of

REPO_SRC = str(Path(__file__).resolve().parent.parent / "src")

QUOTES = [
    "국내 출장 일비는 하루 2만 원으로 한다",
    "식대는 실비를 정산하되 상한을 넘지 못한다",
    "교통비는 대중교통 기준으로 정산한다",
]
BODIES = [
    "출장비는 일비·식대·교통비로 구성된다.",
    "식대 정산은 영수증을 첨부해야 한다.\n\n[[travel-expense-rule]]",
    "교통비는 승차권 사본으로 증빙한다.",
]


@pytest.fixture(autouse=True)
def _subprocess_tmp_guard(tmp_path: Path):
    ensure_subprocess_tmp_ok(tmp_path)


def cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items()}
    base["PYTHONPATH"] = REPO_SRC
    base["EMBEDDING_DISABLED"] = "1"
    base.pop("WIKI_PATH", None)
    base.pop("WIKI_KB_DIR", None)
    if env:
        base.update(env)
    return subprocess.run(
        [sys.executable, "-m", "wiki_mcp", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=base,
        timeout=120,
    )


def cli_json(*args: str) -> dict:
    r = cli(*args)
    assert r.returncode == 0, f"{args}: {r.stderr}{r.stdout}"
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    return json.loads(lines[0])


def wiki_md_files(kb_dir: Path) -> list[str]:
    return sorted(
        p.relative_to(kb_dir / "wiki").as_posix() for p in (kb_dir / "wiki").rglob("*.md")
    )


def folder_hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class TestW2ReviewFlow:
    def test_i1_to_i4_full_flow(self, tmp_path):
        kb_dir = tmp_path / "지식 기반"
        docs = tmp_path / "원본 자료"
        docs.mkdir()
        files: list[Path] = []
        for i, quote in enumerate(QUOTES):
            f = docs / f"규정{i}.md"
            f.write_text(f"# 규정 {i}\n\n{quote}.\n", encoding="utf-8")
            os.utime(f, (time.time() - 30, time.time() - 30))
            files.append(f)
        originals_before = folder_hashes(docs)

        # I1 — CLI 로 init·add-source·scan, MCP 로 초안 3건
        cli_json("kb", "init", "--kb", str(kb_dir))
        cli_json("kb", "add-source", "--kb", str(kb_dir), str(docs))
        scan = cli_json("kb", "scan", "--kb", str(kb_dir))
        assert scan["new"] == 3

        client = McpStdioClient(_base_env({"WIKI_KB_DIR": str(kb_dir)}))
        try:
            # I3 — 플래그 없는 세션의 tools/list 에 approve_draft 가 없다
            tools = _tools_of(client)
            assert "approve_draft" not in tools and "reject_draft" not in tools
            assert {"save_draft", "list_drafts", "get_draft", "mark_orphans"} <= tools

            for i, (quote, body) in enumerate(zip(QUOTES, BODIES)):
                r = client.request(
                    "tools/call",
                    {
                        "name": "save_draft",
                        "arguments": {
                            "kind": "new",
                            "note_id": f"expense-note-{i}",
                            "title": f"정산 규정 {i}",
                            "body": body,
                            "tags": ["정산"],
                            "evidence": [
                                {
                                    "source_id": scan["changes"][i]["source_id"],
                                    "rel_path": f"규정{i}.md",
                                    "quote": quote,
                                }
                            ],
                        },
                    },
                )
                assert r["result"]["isError"] is False, r

            # 이 시점에 wiki 아래 .md 는 0개
            assert wiki_md_files(kb_dir) == []

            listed = cli_json("kb", "drafts-list", "--kb", str(kb_dir))
            assert listed["total"] == 3
            for item in listed["drafts"]:
                shown = cli_json("kb", "drafts-show", "--kb", str(kb_dir), item["id"])
                # MCP 에 넘긴 본문과 바이트로 같다 — 구분자 없음
                idx = int(item["note_id"].rsplit("-", 1)[1])
                assert shown["draft"]["body"] == BODIES[idx]
                assert "<<<" not in shown["draft"]["body"]

            # I2 — CLI 승인 3번 → 출처 붙은 노트 3개
            for item in listed["drafts"]:
                cli_json("kb", "drafts-approve", "--kb", str(kb_dir), item["id"])
            assert wiki_md_files(kb_dir) == [f"concepts/expense-note-{i}.md" for i in range(3)]
            for i in range(3):
                post = frontmatter.load(kb_dir / "wiki" / "concepts" / f"expense-note-{i}.md")
                assert post["kb_sources"], f"expense-note-{i} 의 kb_sources 가 비었다"
                assert post["kb_sources"][0]["sha256"]
            hist = cli_json("kb", "drafts-history", "--kb", str(kb_dir))
            assert hist["total"] == 3
            assert all(e["action"] == "approved" and e["actor"] == "cli" for e in hist["entries"])

            # I3(계속) — 승인된 노트를 같은 MCP 세션의 read_note 로 읽는다.
            # autoindex(watchdog, debounce 2초)가 감지할 때까지 최대 10초 폴링.
            deadline = time.time() + 10
            found = None
            while time.time() < deadline and found is None:
                r = client.request(
                    "tools/call", {"name": "read_note", "arguments": {"note_id": "expense-note-1"}}
                )
                body = r["result"]["content"][0]["text"]
                if "없음" not in body.split("\n", 1)[0]:
                    found = body
                else:
                    time.sleep(0.5)
            assert found is not None
            assert "expense-note-1" in found

            # I4 — 원본 삭제 → missing 표시, 복원 → 해제
            victim = files[0]
            victim_content = victim.read_bytes()
            victim.unlink()
            scan2 = cli_json("kb", "scan", "--kb", str(kb_dir))
            assert scan2["deleted"] == 1
            assert scan2["orphans"]["marked_missing"] == ["expense-note-0"]
            post0 = frontmatter.load(kb_dir / "wiki" / "concepts" / "expense-note-0.md")
            assert post0.get("kb_source_status") == "missing"

            victim.write_bytes(victim_content)
            os.utime(victim, (time.time() - 30, time.time() - 30))
            scan3 = cli_json("kb", "scan", "--kb", str(kb_dir))
            assert "expense-note-0" in scan3["orphans"]["cleared"]
            post0b = frontmatter.load(kb_dir / "wiki" / "concepts" / "expense-note-0.md")
            assert post0b.get("kb_source_status") is None

            # 원본 폴더는 이 흐름 전체에서 (지우고 복원한 것을 빼면) 무변경
            originals_after = folder_hashes(docs)
            assert originals_after == originals_before
        finally:
            client.close()
