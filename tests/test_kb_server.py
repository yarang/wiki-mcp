"""MCP 서버 stdio 핸드셰이크 — KB 모드 도구 노출·기존 도구 보존 검증 (2-6/2-7).

서브프로세스로 서버를 띄워 initialize → tools/list 를 주고받는다.
KB 모드가 아니면 기존 15종과 정확히 같은 도구 목록이 나와야 한다(번들 호환).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import ensure_subprocess_tmp_ok

REPO_SRC = str(Path(__file__).resolve().parent.parent / "src")


@pytest.fixture(autouse=True)
def _subprocess_tmp_guard(tmp_path):
    """이 모듈의 모든 테스트는 자식 프로세스로 MCP 서버를 띄운다.

    자식에는 conftest의 금지 루트 끄기(monkeypatch)가 전달되지 않아,
    Windows의 기본 pytest 임시 폴더(%LOCALAPPDATA%\\Temp)에서는 add_source가
    system_folder로 거부된다. 그 상태라면 이유를 붙여 skip한다(README 개발 절).
    """
    ensure_subprocess_tmp_ok(tmp_path)


LEGACY_TOOLS = {
    "search_wiki",
    "read_note",
    "find_related",
    "list_domains",
    "find_broken_links",
    "suggest_new_note",
    "get_wiki_stats",
    "verify_citations",
    "reindex",
    "create_note",
    "ingest_raw",
    "list_templates",
    "update_note",
    "add_link",
    "delete_note",
}
KB_TOOLS = {
    "list_sources",
    "scan_sources",
    "list_changes",
    "read_extract",
    "kb_status",
}
# WIKI_KB_MCP_MANAGE=1 일 때만 붙는 관리 도구(S1) — 기본은 앱(CLI) 전용
KB_MANAGE_TOOLS = {"add_source", "remove_source"}


def _kb_tools() -> set[str]:
    """KB 모드 도구 — 원본 등록·해제는 MCP 에서 기본 제공하지 않는다(S1)."""
    return LEGACY_TOOLS - {"ingest_raw"} | KB_TOOLS


class McpStdioClient:
    """최소 stdio JSON-RPC 클라이언트 — 줄 단위 JSON."""

    def __init__(self, env: dict):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "wiki_mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        self._id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        line = json.dumps(msg) + "\n"
        self.proc.stdin.write(line.encode("utf-8"))
        self.proc.stdin.flush()
        deadline = time.time() + 60
        while time.time() < deadline:
            raw = self.proc.stdout.readline()
            if not raw:
                raise RuntimeError("서버가 응답 없이 종료됐다")
            data = json.loads(raw.decode("utf-8"))
            if data.get("id") == self._id:
                return data
        raise TimeoutError(f"{method} 응답 대기 시간 초과")

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()

    @property
    def pid_alive(self) -> bool:
        return self.proc.poll() is None


def _base_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()}
    env["PYTHONPATH"] = REPO_SRC
    env["EMBEDDING_DISABLED"] = "1"
    env["WIKI_MCP_TRANSPORT"] = "stdio"
    env.pop("WIKI_PATH", None)
    env.pop("WIKI_KB_DIR", None)
    env.pop("PYTHONIOENCODING", None)
    if extra:
        env.update(extra)
    return env


def _tools_of(client: McpStdioClient) -> set[str]:
    init = client.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    assert "result" in init
    listed = client.request("tools/list")
    return {t["name"] for t in listed["result"]["tools"]}


class TestStdioHandshake:
    def test_no_kb_mode_keeps_legacy_tools_only(self):
        client = McpStdioClient(_base_env())
        try:
            tools = _tools_of(client)
            assert tools == LEGACY_TOOLS
        finally:
            client.close()

    def test_kb_mode_exposes_kb_tools(self, tmp_path):
        kb = tmp_path / "kb"
        kb.mkdir()
        (kb / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
        (kb / ".kb").mkdir()
        client = McpStdioClient(_base_env({"WIKI_KB_DIR": str(kb)}))
        try:
            tools = _tools_of(client)
            # KB 모드: ingest_raw 는 raw 폴더 경로가 KB 안쪽으로 바뀌어 빠지고(S5)
            # 원본 등록·해제도 기본 제공하지 않는다(S1) — 앱(CLI)이 관리한다.
            assert tools == _kb_tools()
        finally:
            client.close()

    def test_kb_manage_env_exposes_source_registration(self, tmp_path):
        """WIKI_KB_MCP_MANAGE=1 이면 add_source·remove_source 가 도구로 붙는다(S1)."""
        kb = tmp_path / "kb"
        kb.mkdir()
        (kb / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
        (kb / ".kb").mkdir()
        client = McpStdioClient(_base_env({"WIKI_KB_DIR": str(kb), "WIKI_KB_MCP_MANAGE": "1"}))
        try:
            tools = _tools_of(client)
            assert tools == _kb_tools() | KB_MANAGE_TOOLS
        finally:
            client.close()

    def test_mcp_add_source_has_no_confirm_param(self, tmp_path):
        """MCP add_source 에는 confirm 인자가 없다 — 민감 경로는 앱에서만 등록한다(S1)."""
        kb = tmp_path / "kb"
        kb.mkdir()
        (kb / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
        (kb / ".kb").mkdir()
        client = McpStdioClient(_base_env({"WIKI_KB_DIR": str(kb), "WIKI_KB_MCP_MANAGE": "1"}))
        try:
            _tools_of(client)  # initialize 먼저
            listed = client.request("tools/list")
            add = [t for t in listed["result"]["tools"] if t["name"] == "add_source"][0]
            props = add.get("inputSchema", {}).get("properties", {})
            assert "confirm" not in props
        finally:
            client.close()

    def test_mcp_add_source_sensitive_path_errors(self, tmp_path):
        """민감 이름 경로는 MCP 등록 불가 — '앱에서 등록하세요' 오류(S1)."""
        kb = tmp_path / "kb"
        kb.mkdir()
        (kb / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
        (kb / ".kb").mkdir()
        payroll = tmp_path / "급여"
        payroll.mkdir()
        client = McpStdioClient(_base_env({"WIKI_KB_DIR": str(kb), "WIKI_KB_MCP_MANAGE": "1"}))
        try:
            _tools_of(client)
            r = client.request(
                "tools/call",
                {"name": "add_source", "arguments": {"path": str(payroll)}},
            )
            assert r["result"]["isError"] is True
            assert "sensitive_name" in r["result"]["content"][0]["text"]
            assert "앱" in r["result"]["content"][0]["text"]
        finally:
            client.close()

    def test_kb_uninitialized_keeps_legacy_tools_only(self, tmp_path):
        # 초기화되지 않은 KB 폴더 — KB 도구 없이 기존 도구만(서버는 뜬다).
        # WIKI_KB_DIR 이 설정됐으니 ingest_raw 는 빠진다(S5).
        empty = tmp_path / "empty-kb"
        empty.mkdir()
        client = McpStdioClient(_base_env({"WIKI_KB_DIR": str(empty)}))
        try:
            tools = _tools_of(client)
            assert tools == LEGACY_TOOLS - {"ingest_raw"}
        finally:
            client.close()

    def test_kb_uninitialized_creates_no_kb_folders(self, tmp_path):
        """미초기화 KB로 서버를 띄워도 .kb/·wiki/를 만들지 않는다(루트 오염 방지)."""
        empty = tmp_path / "empty-kb"
        empty.mkdir()
        client = McpStdioClient(_base_env({"WIKI_KB_DIR": str(empty)}))
        try:
            _tools_of(client)
        finally:
            client.close()
        assert list(empty.iterdir()) == []

    def test_kb_mode_scan_via_tool_and_search_scope(self, tmp_path):
        """KB 도구 호출 회로 — scan_sources → search_wiki(scope=sources)."""
        kb = tmp_path / "kb"
        kb.mkdir()
        (kb / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
        (kb / ".kb").mkdir()
        docs = tmp_path / "docs"
        docs.mkdir()
        f = docs / "기준문서.md"
        f.write_text("# 기준\n\n출장비 정산 기준을 담은 문서 본문입니다.", encoding="utf-8")
        old = time.time() - 30
        os.utime(f, (old, old))

        client = McpStdioClient(_base_env({"WIKI_KB_DIR": str(kb), "WIKI_KB_MCP_MANAGE": "1"}))
        try:
            _tools_of(client)  # initialize
            add = client.request(
                "tools/call",
                {
                    "name": "add_source",
                    "arguments": {"path": str(docs)},
                },
            )
            assert "source" in add["result"]["content"][0]["text"]
            scan = client.request(
                "tools/call",
                {
                    "name": "scan_sources",
                    "arguments": {},
                },
            )
            scan_out = json.loads(scan["result"]["content"][0]["text"])
            assert scan_out["new"] == 1 and scan_out["extracted"] == 1
            search = client.request(
                "tools/call",
                {
                    "name": "search_wiki",
                    "arguments": {"query": "정산 기준", "scope": "sources", "top_k": 3},
                },
            )
            body = search["result"]["content"][0]["text"]
            assert "기준문서.md" in body
            assert "[원본]" in body
        finally:
            client.close()


# ─── H4: 서버 실행 중 CLI 변경의 즉시 반영 ──────────────────────────
class TestServerFreshnessWithCli:
    def test_cli_scan_and_remove_reflected_in_live_search(self, tmp_path):
        """떠 있는 서버에 CLI 로 scan·remove-source 한 결과가 search_wiki 에 반영된다."""
        import subprocess

        kb = tmp_path / "kb"
        kb.mkdir()
        (kb / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
        (kb / ".kb").mkdir()
        docs = tmp_path / "docs"
        docs.mkdir()
        f = docs / "정산지침.md"
        f.write_text("# 지침\n\n출장비 정산 기준을 담은 문서 본문입니다.", encoding="utf-8")
        old = time.time() - 30
        os.utime(f, (old, old))

        client = McpStdioClient(_base_env({"WIKI_KB_DIR": str(kb)}))
        try:
            _tools_of(client)  # initialize — 색인 백그라운드 시작

            def search_sources(query: str) -> str:
                r = client.request(
                    "tools/call",
                    {
                        "name": "search_wiki",
                        "arguments": {"query": query, "scope": "sources", "top_k": 3},
                    },
                )
                return r["result"]["content"][0]["text"]

            def cli(*args: str):
                return subprocess.run(
                    [sys.executable, "-m", "wiki_mcp", *args],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    env=_base_env(),
                    timeout=120,
                )

            # 1) CLI 로 원본 등록 + 스캔 → 살아 있는 서버의 검색에 바로 잡힌다
            assert cli("kb", "add-source", "--kb", str(kb), str(docs)).returncode == 0
            scan = cli("kb", "scan", "--kb", str(kb))
            assert scan.returncode == 0, scan.stderr
            body = search_sources("정산 기준")
            assert "정산지침.md" in body
            assert "<<<source" in body  # M15: 원문 구분자

            # 2) CLI 로 등록 해제 → 조각이 즉시 사라진다
            sid = json.loads(cli("kb", "list-sources", "--kb", str(kb)).stdout)["sources"][0]["id"]
            assert cli("kb", "remove-source", "--kb", str(kb), sid).returncode == 0
            body2 = search_sources("정산 기준")
            assert "정산지침.md" not in body2
        finally:
            client.close()


# ─── M1: KB 꺼짐 — search_wiki 출력은 0.2.4 와 같다 ───────────────────
class TestLegacySearchOutput:
    def test_no_kb_output_matches_024_format(self, tmp_path):
        wiki = tmp_path / "wiki"
        (wiki / "llm").mkdir(parents=True)
        (wiki / "llm" / "rag.md").write_text(
            '---\nid: rag\ntitle: 검색 증강 생성\nsource_url: ""\ntags: [rag]\n---\n\n'
            "검색 증강 생성은 근거를 검색해 답에 인용하는 방식이다.",
            encoding="utf-8",
        )
        client = McpStdioClient(_base_env({"WIKI_PATH": str(wiki)}))
        try:
            _tools_of(client)
            r = client.request(
                "tools/call",
                {"name": "search_wiki", "arguments": {"query": "검색 증강"}},
            )
            body = r["result"]["content"][0]["text"]
            assert "[[rag]]" in body
            assert "**점수**" in body  # 점수 필드 유지
            assert "scope" not in body  # KB 꺼짐 — scope 표기 없음(0.2.4 동일)
            assert "<<<source" not in body
            # 빈 결과 문구도 0.2.4 그대로
            r2 = client.request(
                "tools/call",
                {"name": "search_wiki", "arguments": {"query": "전혀관없는질의어"}},
            )
            body2 = r2["result"]["content"][0]["text"]
            assert body2.startswith('검색 결과 없음 (query: "전혀관없는질의어", tag: None)')
            assert "scope" not in body2
        finally:
            client.close()


# ─── H5: 도구 결과의 changes 상한 ────────────────────────────────────
class TestToolChangesCap:
    def test_scan_tool_truncates_changes_over_50(self, tmp_path):
        kb = tmp_path / "kb"
        kb.mkdir()
        (kb / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
        (kb / ".kb").mkdir()
        docs = tmp_path / "docs"
        docs.mkdir()
        old = time.time() - 30
        for n in range(55):
            f = docs / f"doc{n:03d}.md"
            f.write_text(f"# 문서 {n}\n\n{n}번 문서의 본문 내용입니다.", encoding="utf-8")
            os.utime(f, (old, old))

        client = McpStdioClient(_base_env({"WIKI_KB_DIR": str(kb), "WIKI_KB_MCP_MANAGE": "1"}))
        try:
            _tools_of(client)
            client.request(
                "tools/call",
                {"name": "add_source", "arguments": {"path": str(docs)}},
            )
            scan = client.request("tools/call", {"name": "scan_sources", "arguments": {}})
            out = json.loads(scan["result"]["content"][0]["text"])
            assert out["new"] == 55  # 카운터는 전체
            assert len(out["changes"]) == 50  # 목록은 상한
            assert out["truncated"] is True
            assert "list_changes" in out["truncated_message"]
        finally:
            client.close()
