"""wiki-mcp 진입점 — 인자 없으면 MCP 서버(stdio), `kb` 하위 명령은 앱용 JSON CLI.

JSON 계약(docs/cli-contract.md):
    성공      → 결과 JSON 1개를 stdout 에, 종료 코드 0
    입력 오류  → {"error": {"code", "message"}}, 종료 코드 1
    내부 오류  → {"error": {"code": "internal", ...}}, 종료 코드 2
    경고      → {"warning": {...}} (등록 유보), 종료 코드 3

stdout 은 항상 UTF-8 로 고정한다 — Windows cp1252 콘솔에서도 한글 경로가
깨지거나 UnicodeEncodeError 가 나지 않게 한다. 로그는 stderr 로만.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_INPUT = 1
EXIT_INTERNAL = 2
EXIT_WARNING = 3


def _emit(payload: dict, code: int) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return code


def _force_utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass  # 리다이렉트된 스트림 등 — 이미 바이트를 그대로 쓴다


def _kb_from(args: argparse.Namespace):
    from wiki_mcp.kb.store import KnowledgeBase

    return KnowledgeBase(Path(args.kb))


def _cmd_init(args) -> dict:
    return _kb_from(args).init()


def _cmd_add_source(args) -> dict:
    from wiki_mcp.kb.sources import add_source

    return add_source(
        _kb_from(args),
        args.path,
        include=args.include,
        exclude=args.exclude,
        confirm=args.confirm,
    )


def _cmd_remove_source(args) -> dict:
    from wiki_mcp.kb.sources import remove_source

    return remove_source(_kb_from(args), args.source_id)


def _cmd_list_sources(args) -> dict:
    from wiki_mcp.kb.sources import list_sources

    return list_sources(_kb_from(args))


def _cmd_scan(args) -> dict:
    from wiki_mcp import config
    from wiki_mcp.kb.scanner import scan_sources

    kb = _kb_from(args)
    return scan_sources(
        kb,
        ocr=config.INGEST_OCR,
        ocr_bin=config.INGEST_OCR_BIN or None,
        max_extract_chars=config.KB_MAX_EXTRACT_CHARS,
    )


def _cmd_status(args) -> dict:
    from wiki_mcp.kb.status import kb_status

    return kb_status(_kb_from(args))


# ─── 초안 검토 대기열 (0.4.0) ────────────────────────────────────────
def _cmd_drafts_list(args) -> dict:
    from wiki_mcp.kb.drafts import list_drafts

    return list_drafts(_kb_from(args), limit=args.limit, offset=args.offset)


def _cmd_drafts_show(args) -> dict:
    from wiki_mcp.kb.drafts import show_draft

    return show_draft(_kb_from(args), args.draft_id, context_chars=args.context)


def _cmd_drafts_approve(args) -> dict:
    from wiki_mcp.kb.drafts import approve_draft

    return approve_draft(_kb_from(args), args.draft_id, actor="cli")


def _cmd_drafts_reject(args) -> dict:
    from wiki_mcp.kb.drafts import reject_draft, reject_stale_drafts
    from wiki_mcp.kb.errors import KBError

    if args.all_stale and args.draft_id:
        raise KBError("invalid_arguments", "--all-stale 와 DRAFT_ID 는 함께 쓸 수 없다")
    if not args.all_stale and not args.draft_id:
        raise KBError("invalid_arguments", "DRAFT_ID 나 --all-stale 중 하나를 지정한다")
    kb = _kb_from(args)
    if args.all_stale:
        return reject_stale_drafts(kb, actor="cli", reason=args.reason)
    return reject_draft(kb, args.draft_id, actor="cli", reason=args.reason)


def _cmd_drafts_history(args) -> dict:
    from wiki_mcp.kb.history import list_history

    return list_history(_kb_from(args), limit=args.limit, offset=args.offset)


MAX_DRAFT_INPUT_BYTES = 1_048_576


def _cmd_drafts_save(args) -> dict:
    """초안 저장(스크립트·앱 e2e용) — --input 파일은 DraftInput JSON(UTF-8)."""
    from pydantic import ValidationError

    from wiki_mcp.kb.drafts import DraftInput, save_draft
    from wiki_mcp.kb.errors import KBError

    p = Path(args.input)
    with open(p, "rb") as fh:  # 상한+1 바이트만 읽는다 — 무한 파일을 다 읽지 않게
        data = fh.read(MAX_DRAFT_INPUT_BYTES + 1)
    if len(data) > MAX_DRAFT_INPUT_BYTES:
        raise KBError("draft_too_large", f"--input 파일이 1MB를 넘는다({len(data)}바이트)")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise KBError("invalid_draft", f"--input JSON 파싱 실패: {e}") from e
    try:
        draft_input = DraftInput.model_validate(payload)
    except ValidationError as e:
        raise KBError("invalid_draft", f"--input 초안 필드가 잘못됐다: {e}") from e
    return save_draft(_kb_from(args), draft_input, actor="cli")


def _cmd_mark_orphans(args) -> dict:
    from wiki_mcp.kb.orphans import mark_orphans

    return mark_orphans(_kb_from(args))


class _JSONArgumentParser(argparse.ArgumentParser):
    """인자 오류도 JSON 계약으로 — stderr 안내 + error JSON, 종료 코드 1."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        _emit({"error": {"code": "invalid_arguments", "message": message}}, EXIT_INPUT)
        sys.exit(EXIT_INPUT)


def build_parser() -> argparse.ArgumentParser:
    parser = _JSONArgumentParser(
        prog="wiki-mcp",
        description="LLM Wiki MCP 서버 — 인자 없이 실행하면 stdio MCP 서버, "
        "`kb` 하위 명령은 지식 기반 관리용 JSON CLI.",
    )
    sub = parser.add_subparsers(dest="command", parser_class=_JSONArgumentParser)
    kb = sub.add_parser("kb", help="지식 기반(원본 보존 모드) 관리")
    kb_cmd = kb.add_subparsers(dest="kb_command", required=True, parser_class=_JSONArgumentParser)

    p = kb_cmd.add_parser("init", help="빈 KB 폴더 구조를 만든다")
    p.add_argument("--kb", required=True)
    p.set_defaults(fn=_cmd_init)

    p = kb_cmd.add_parser("add-source", help="원본 폴더 등록")
    p.add_argument("--kb", required=True)
    p.add_argument("path")
    p.add_argument("--include", action="append", default=[], help="포함 glob (여러 번 가능)")
    p.add_argument("--exclude", action="append", default=[], help="제외 glob (여러 번 가능)")
    p.add_argument("--confirm", action="store_true", help="민감 이름 경고를 확인하고 등록")
    p.set_defaults(fn=_cmd_add_source)

    p = kb_cmd.add_parser("remove-source", help="원본 등록 제거(원본은 건드리지 않음)")
    p.add_argument("--kb", required=True)
    p.add_argument("source_id")
    p.set_defaults(fn=_cmd_remove_source)

    p = kb_cmd.add_parser("list-sources", help="등록된 원본 목록")
    p.add_argument("--kb", required=True)
    p.set_defaults(fn=_cmd_list_sources)

    p = kb_cmd.add_parser("scan", help="모든 원본 스캔(추출 갱신)")
    p.add_argument("--kb", required=True)
    p.set_defaults(fn=_cmd_scan)

    p = kb_cmd.add_parser("status", help="원본별 상태 요약")
    p.add_argument("--kb", required=True)
    p.set_defaults(fn=_cmd_status)

    p = kb_cmd.add_parser("drafts-list", help="대기 중인 노트 초안 목록")
    p.add_argument("--kb", required=True)
    p.add_argument("--limit", type=int, default=50, help="최대 건수(기본 50, 상한 200)")
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(fn=_cmd_drafts_list)

    p = kb_cmd.add_parser("drafts-show", help="초안 상세(본문·근거·문맥·현재 노트 원문 그대로)")
    p.add_argument("--kb", required=True)
    p.add_argument("draft_id")
    p.add_argument("--context", type=int, default=600, help="근거 문맥 길이(기본 600, 상한 2000)")
    p.set_defaults(fn=_cmd_drafts_show)

    p = kb_cmd.add_parser("drafts-approve", help="초안 승인 — wiki 노트로 반영")
    p.add_argument("--kb", required=True)
    p.add_argument("draft_id")
    p.set_defaults(fn=_cmd_drafts_approve)

    p = kb_cmd.add_parser(
        "drafts-reject",
        help="초안 반려(--reason=<사유> 한 토큰 형식 지원) 또는 --all-stale 일괄 반려",
    )
    p.add_argument("--kb", required=True)
    p.add_argument("draft_id", nargs="?")
    p.add_argument("--reason", default="")
    p.add_argument(
        "--all-stale",
        action="store_true",
        help="issues 에 draft_stale 이 있는 초안을 모두 반려한다(대기열 선점 해소)",
    )
    p.set_defaults(fn=_cmd_drafts_reject)

    p = kb_cmd.add_parser("drafts-history", help="승인·반려 이력(최신순)")
    p.add_argument("--kb", required=True)
    p.add_argument("--limit", type=int, default=50, help="최대 건수(기본 50, 상한 500)")
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(fn=_cmd_drafts_history)

    p = kb_cmd.add_parser(
        "drafts-save", help="초안 저장(스크립트·앱 e2e용) — --input 은 DraftInput JSON"
    )
    p.add_argument("--kb", required=True)
    p.add_argument("--input", required=True)
    p.set_defaults(fn=_cmd_drafts_save)

    p = kb_cmd.add_parser("mark-orphans", help="연결 원본 소실 표시 갱신(scan 이 끝나도 자동 실행)")
    p.add_argument("--kb", required=True)
    p.set_defaults(fn=_cmd_mark_orphans)

    return parser


def run_cli(argv: list[str]) -> int:
    """kb 하위 명령 실행 — JSON 을 stdout 에 내고 종료 코드를 돌려준다."""
    from wiki_mcp.kb.errors import KBError

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = args.fn(args)
    except KBError as e:
        return _emit({"error": {"code": e.code, "message": e.message}}, EXIT_INPUT)
    except OSError as e:
        return _emit({"error": {"code": "io_error", "message": str(e)}}, EXIT_INPUT)
    except Exception as e:  # 내부 오류 — 상세는 stderr 로만, JSON 은 일반 메시지(M7)
        print(f"wiki-mcp kb internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return _emit(
            {"error": {"code": "internal", "message": "내부 오류 — 로그(stderr)를 확인한다"}},
            EXIT_INTERNAL,
        )
    if "warning" in payload:
        return _emit(payload, EXIT_WARNING)
    return _emit(payload, EXIT_OK)


def main(argv: list[str] | None = None) -> int | None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        # 인자 없음 — MCP 서버(stdio). 기존 동작과 완전히 같다.
        from wiki_mcp.server import main as server_main

        return server_main()

    _force_utf8_stdout()
    if argv[0] in ("-h", "--help"):
        build_parser().parse_args(argv)  # 도움말 출력 후 종료(0)
        return EXIT_OK
    if argv[0] != "kb":
        print(
            f"알 수 없는 명령: {argv[0]} — `wiki-mcp`(MCP 서버) 또는 `wiki-mcp kb ...`",
            file=sys.stderr,
        )
        return _emit({"error": {"code": "unknown_command", "message": argv[0]}}, EXIT_INPUT)
    return run_cli(argv)


if __name__ == "__main__":
    sys.exit(main())
