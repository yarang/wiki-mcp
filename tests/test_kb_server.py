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

REPO_SRC = str(Path(__file__).resolve().parent.parent / "src")

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
    "add_source",
    "list_sources",
    "remove_source",
    "scan_sources",
    "list_changes",
    "read_extract",
    "kb_status",
}


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
            assert KB_TOOLS <= tools
            assert LEGACY_TOOLS <= tools
            # 기존 동작 보존 + KB 7종 추가
            assert tools == LEGACY_TOOLS | KB_TOOLS
        finally:
            client.close()

    def test_kb_uninitialized_keeps_legacy_tools_only(self, tmp_path):
        # 초기화되지 않은 KB 폴더 — KB 도구 없이 기존 15종(서버는 뜬다)
        empty = tmp_path / "empty-kb"
        empty.mkdir()
        client = McpStdioClient(_base_env({"WIKI_KB_DIR": str(empty)}))
        try:
            tools = _tools_of(client)
            assert tools == LEGACY_TOOLS
        finally:
            client.close()

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

        client = McpStdioClient(_base_env({"WIKI_KB_DIR": str(kb)}))
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
