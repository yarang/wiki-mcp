"""CLI 계약 — `python -m wiki_mcp kb ...` (3장).

서브프로세스로 실제 진입점을 실행해 JSON 출력·종료 코드·UTF-8 동작을 검증한다.
PYTHONIOENCODING 을 env 에서 빼서도 한글이 깨지지 않는지 본다(Windows 콘솔
cp1252 상황의 근사 — 이 환경에선 UTF-8 로 강제되는지, 예외가 없는지가 판정 기준).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_SRC = str(Path(__file__).resolve().parent.parent / "src")


def run_cli(
    args: list[str], *, env_extra: dict | None = None, drop_ioencoding: bool = True
) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()}
    if drop_ioencoding:
        env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    env["PYTHONPATH"] = REPO_SRC  # src 레이아웃 — 서브프로세스가 wiki_mcp 를 찾게
    env["EMBEDDING_DISABLED"] = "1"  # 임베딩 서버 접근 시도 방지
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "wiki_mcp", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )


def parse_stdout(r: subprocess.CompletedProcess) -> dict:
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"stdout 에는 JSON 한 줄만 (got {len(lines)}): {r.stdout!r}"
    return json.loads(lines[0])


@pytest.fixture
def kb_dir(tmp_path: Path) -> str:
    return str(tmp_path / "지식기반")  # 한글 경로 — Windows 사용자 이름 폴더 근사


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    d = tmp_path / "내문서"
    d.mkdir()
    f = d / "메모.md"
    f.write_text("# 메모\n\n한글 파일 이름과 내용이 있는 문서 본문입니다.", encoding="utf-8")
    old = __import__("time").time() - 30
    os.utime(f, (old, old))  # unstable(수정 직후) 스킵 회피
    return d


class TestInit:
    def test_init_emits_single_json_and_creates_structure(self, kb_dir):
        r = run_cli(["kb", "init", "--kb", kb_dir])
        assert r.returncode == 0, r.stderr
        out = parse_stdout(r)
        assert out["kb_dir"] == kb_dir
        assert (Path(kb_dir) / "sources.yaml").is_file()
        assert (Path(kb_dir) / ".kb" / "extract").is_dir()
        assert (Path(kb_dir) / "wiki").is_dir()

    def test_init_idempotent(self, kb_dir):
        run_cli(["kb", "init", "--kb", kb_dir])
        r = run_cli(["kb", "init", "--kb", kb_dir])
        assert r.returncode == 0
        assert parse_stdout(r)["created"] == []


class TestAddSource:
    def test_add_then_list(self, kb_dir, docs):
        run_cli(["kb", "init", "--kb", kb_dir])
        r = run_cli(["kb", "add-source", "--kb", kb_dir, str(docs)])
        assert r.returncode == 0, r.stderr
        src = parse_stdout(r)["source"]
        assert src["path"] == str(docs.resolve())

        r = run_cli(["kb", "list-sources", "--kb", kb_dir])
        out = parse_stdout(r)
        assert out["sources"][0]["id"] == src["id"]
        assert json.dumps(str(docs), ensure_ascii=False) in r.stdout  # 한글 경로 UTF-8 유지

    def test_missing_path_is_input_error(self, kb_dir, tmp_path):
        run_cli(["kb", "init", "--kb", kb_dir])
        r = run_cli(["kb", "add-source", "--kb", kb_dir, str(tmp_path / "없음")])
        assert r.returncode == 1
        out = parse_stdout(r)
        assert out["error"]["code"] == "source_not_found"
        assert out["error"]["message"]

    def test_sensitive_folder_warns_exit_3_then_confirm(self, kb_dir, tmp_path):
        run_cli(["kb", "init", "--kb", kb_dir])
        payroll = tmp_path / "급여"
        payroll.mkdir()
        r = run_cli(["kb", "add-source", "--kb", kb_dir, str(payroll)])
        assert r.returncode == 3
        out = parse_stdout(r)
        assert out["warning"]["code"] == "sensitive_name"
        # 등록 유보 확인
        r2 = run_cli(["kb", "list-sources", "--kb", kb_dir])
        assert parse_stdout(r2)["sources"] == []
        # --confirm 이면 등록
        r3 = run_cli(["kb", "add-source", "--kb", kb_dir, str(payroll), "--confirm"])
        assert r3.returncode == 0
        assert "source" in parse_stdout(r3)

    def test_include_exclude_flags(self, kb_dir, docs):
        run_cli(["kb", "init", "--kb", kb_dir])
        r = run_cli(
            [
                "kb",
                "add-source",
                "--kb",
                kb_dir,
                str(docs),
                "--include",
                "*.hwpx",
                "--exclude",
                "drafts/*",
            ]
        )
        src = parse_stdout(r)["source"]
        assert src["include"] == ["*.hwpx"]
        assert src["exclude"] == ["drafts/*"]

    def test_remove_source(self, kb_dir, docs):
        run_cli(["kb", "init", "--kb", kb_dir])
        src = parse_stdout(run_cli(["kb", "add-source", "--kb", kb_dir, str(docs)]))["source"]
        r = run_cli(["kb", "remove-source", "--kb", kb_dir, src["id"]])
        assert r.returncode == 0
        assert parse_stdout(r)["removed"] == src["id"]
        assert parse_stdout(run_cli(["kb", "list-sources", "--kb", kb_dir]))["sources"] == []

    def test_remove_unknown_is_input_error(self, kb_dir):
        run_cli(["kb", "init", "--kb", kb_dir])
        r = run_cli(["kb", "remove-source", "--kb", kb_dir, "src-nope"])
        assert r.returncode == 1
        assert parse_stdout(r)["error"]["code"] == "source_not_found"


class TestScanStatus:
    def test_scan_then_status(self, kb_dir, docs):
        run_cli(["kb", "init", "--kb", kb_dir])
        run_cli(["kb", "add-source", "--kb", kb_dir, str(docs)])
        r = run_cli(["kb", "scan", "--kb", kb_dir])
        assert r.returncode == 0, r.stderr
        out = parse_stdout(r)
        assert out["new"] == 1 and out["extracted"] == 1
        assert out["changes"][0]["rel_path"] == "메모.md"

        r = run_cli(["kb", "status", "--kb", kb_dir])
        status = parse_stdout(r)
        assert status["sources"][0]["files"] == 1
        assert status["sources"][0]["by_status"] == {"ok": 1}
        assert status["last_scan"]["results"]["new"] == 1
        assert status["last_scan"]["scanned_at"]

    def test_scan_before_init_is_input_error(self, kb_dir):
        r = run_cli(["kb", "scan", "--kb", kb_dir])
        assert r.returncode == 1
        assert parse_stdout(r)["error"]["code"] == "kb_not_initialized"

    def test_scan_reports_unsupported_hwp(self, kb_dir, docs):
        h = docs / "legacy.hwp"
        h.write_bytes(b"HWP")
        old = __import__("time").time() - 30
        os.utime(h, (old, old))
        run_cli(["kb", "init", "--kb", kb_dir])
        run_cli(["kb", "add-source", "--kb", kb_dir, str(docs)])
        r = run_cli(["kb", "scan", "--kb", kb_dir])
        out = parse_stdout(r)
        assert out["failed"] == 1
        change = [c for c in out["changes"] if c["rel_path"] == "legacy.hwp"][0]
        assert change["reason"] == "unsupported"
        status = parse_stdout(run_cli(["kb", "status", "--kb", kb_dir]))
        failed = status["sources"][0]["failed"]
        assert failed == [{"rel_path": "legacy.hwp", "reason": "unsupported"}]


class TestCliContract:
    def test_unknown_command_is_input_error(self):
        r = run_cli(["frobnicate"])
        assert r.returncode == 1
        assert parse_stdout(r)["error"]["code"] == "unknown_command"

    def test_no_logs_on_stdout(self, kb_dir, docs):
        run_cli(["kb", "init", "--kb", kb_dir])
        run_cli(["kb", "add-source", "--kb", kb_dir, str(docs)])
        r = run_cli(["kb", "scan", "--kb", kb_dir])
        parse_stdout(r)  # 정확히 JSON 한 줄 — 로그가 섞이면 여기서 실패

    def test_korean_output_survives_c_locale(self, kb_dir, docs):
        """로캘·인코딩 환경변수 없이도 한글 JSON 이 깨지지 않는다."""
        run_cli(["kb", "init", "--kb", kb_dir])
        r = run_cli(
            ["kb", "add-source", "--kb", kb_dir, str(docs)], env_extra={"LC_ALL": "C", "LANG": "C"}
        )
        assert r.returncode == 0
        out = parse_stdout(r)
        assert out["source"]["path"] == str(docs.resolve())

    def test_help_mentions_kb(self):
        r = run_cli(["--help"])
        assert r.returncode == 0
        assert "kb" in r.stdout


class TestConfigResolution:
    """0-1/KB 설정 — WIKI_KB_DIR이 WIKI_PATH·DATA_DIR 기본값을 바꾼다(서브프로세스로
    import 시점 평가를 검증). 명시적 WIKI_PATH가 우선한다."""

    def _config(self, env_extra: dict) -> dict:
        env = {k: v for k, v in os.environ.items()}
        env["PYTHONPATH"] = REPO_SRC
        env.pop("WIKI_PATH", None)
        env.pop("WIKI_KB_DIR", None)
        env.update(env_extra)
        code = (
            "from wiki_mcp import config; "
            "import json; "
            "print(json.dumps({'wiki': str(config.WIKI_PATH), "
            "'data': str(config.DATA_DIR), 'kb': str(config.KB_DIR), "
            "'kb_on': config.KB_ENABLED}))"
        )
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
        )
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_kb_dir_sets_default_wiki_and_data(self, tmp_path):
        kb = tmp_path / "kb"
        out = self._config({"WIKI_KB_DIR": str(kb)})
        assert out["kb"] == str(kb.resolve())
        assert out["kb_on"] is True
        assert out["wiki"] == str((kb / "wiki").resolve())
        assert out["data"] == str((kb / ".kb" / "index").resolve())

    def test_explicit_wiki_path_wins(self, tmp_path):
        kb = tmp_path / "kb"
        explicit = tmp_path / "my-wiki"
        out = self._config({"WIKI_KB_DIR": str(kb), "WIKI_PATH": str(explicit)})
        assert out["wiki"] == str(explicit.resolve())
        assert out["kb_on"] is True  # KB 모드는 켜져 있다

    def test_no_kb_dir_keeps_legacy_defaults(self, tmp_path):
        out = self._config({"WIKI_PATH": str(tmp_path / "w")})
        assert out["kb_on"] is False
        assert out["wiki"] == str((tmp_path / "w").resolve())
        # H3: 0.2.4 회귀 — DATA_DIR 기본값은 <저장소>/data (저장소 밖이 아님)
        import wiki_mcp

        repo_root = Path(wiki_mcp.__file__).resolve().parent.parent.parent
        assert out["data"] == str((repo_root / "data").resolve())
