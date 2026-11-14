"""환경 변수 + 기본값."""
import os
from pathlib import Path

# Package directory: .../tools/wiki-mcp/src/wiki_mcp/
_PKG_DIR = Path(__file__).resolve().parent

# Bundled skeleton ships in the source tree (NOT inside the installed package):
#   src/wiki_mcp/ -> src/ -> tools/wiki-mcp/ -> examples/sample-wiki
# Reachable for editable installs (`pip install -e .`) and git clones. For
# regular pip installs the path resolves into site-packages and the user is
# expected to set WIKI_PATH explicitly.
_SAMPLE_WIKI = _PKG_DIR.parent.parent / "examples" / "sample-wiki"


def _default_wiki_path() -> Path:
    """Pick a sensible default WIKI_PATH when the env var is unset.

    Falls back to the bundled sample-wiki skeleton if present, else an empty
    Path — the indexer will build an empty index and log a notice in that case.
    """
    if _SAMPLE_WIKI.exists():
        return _SAMPLE_WIKI.resolve()
    return Path("")


# @MX:NOTE: [AUTO] WIKI_PATH resolution order — explicit env var wins, then bundled sample-wiki
_env_wiki = os.environ.get("WIKI_PATH", "").strip()
if _env_wiki:
    WIKI_PATH = Path(_env_wiki).expanduser().resolve()
else:
    WIKI_PATH = _default_wiki_path()

DATA_DIR = Path(os.environ.get(
    "WIKI_DATA_DIR",
    str(Path(__file__).parent.parent.parent / "data")
)).expanduser().resolve()

# 노트 템플릿 디렉토리 — 원하는 양식(frontmatter 필드 + 섹션 구조)을 .md 파일로 정의.
# 기본: <wiki>/.templates (숨김 디렉토리 → 인덱서가 노트로 잡지 않도록 제외됨).
_env_tmpl = os.environ.get("WIKI_TEMPLATE_DIR", "").strip()
if _env_tmpl:
    TEMPLATE_DIR = Path(_env_tmpl).expanduser().resolve()
elif str(WIKI_PATH):
    TEMPLATE_DIR = (WIKI_PATH / ".templates").resolve()
else:
    TEMPLATE_DIR = Path("")

# create_note에서 template 미지정 시 적용할 기본 템플릿 이름 (빈 값 = 템플릿 미사용).
DEFAULT_TEMPLATE = os.environ.get("WIKI_DEFAULT_TEMPLATE", "").strip()

# ─── raw → wiki ingestion ────────────────────────────────────────────────
# raw 원문을 두는 디렉토리. 기본: <wiki 상위>/raw (wiki 밖 → 노트로 인덱싱되지 않음).
_env_raw = os.environ.get("WIKI_RAW_DIR", "").strip()
if _env_raw:
    RAW_DIR = Path(_env_raw).expanduser().resolve()
elif str(WIKI_PATH):
    RAW_DIR = (WIKI_PATH.parent / "raw").resolve()
else:
    RAW_DIR = Path("")

# ingestion 활성화 (기본 off — 미설정 시 watch는 기존처럼 재인덱싱만 수행).
INGEST_ENABLED = os.environ.get("WIKI_INGEST_ENABLED", "").lower() in ("1", "true", "yes")

# 생성 LLM 엔드포인트 — OpenAI 호환 chat completions (Open WebUI 기본 :8080).
INGEST_URL = os.environ.get(
    "WIKI_INGEST_URL", "http://localhost:8080/api/chat/completions"
)
INGEST_MODEL = os.environ.get("WIKI_INGEST_MODEL", "").strip()
INGEST_API_KEY = os.environ.get("WIKI_INGEST_API_KEY", "").strip()
INGEST_TIMEOUT = int(os.environ.get("WIKI_INGEST_TIMEOUT", "120"))

# 생성 노트에 적용할 템플릿 (빈 값 = 템플릿 없이 LLM 본문 그대로 사용).
INGEST_TEMPLATE = os.environ.get("WIKI_INGEST_TEMPLATE", "").strip()

# 긴 문서 청킹 — 이 길이(문자) 초과 시 아웃라인→작성(다중 노트) 경로 사용.
# 0 이하이면 청킹 비활성화(항상 단일 노트).
INGEST_MAX_CHARS = int(os.environ.get("WIKI_INGEST_MAX_CHARS", "6000"))
# 문서당 생성 노트 상한 (폭주 방지).
INGEST_MAX_NOTES = int(os.environ.get("WIKI_INGEST_MAX_NOTES", "20"))

# raw로 취급할 확장자 (그 외 파일은 무시). 텍스트 + 문서(추출 지원) 포맷.
INGEST_EXTENSIONS = tuple(
    (e if e.startswith(".") else f".{e}").lower()
    for e in os.environ.get(
        "WIKI_INGEST_EXT",
        ".txt,.md,.markdown,.text,.pdf,.docx,.pptx,.xlsx,.html,.htm,.png,.jpg,.jpeg",
    ).split(",")
    if e.strip()
)

# 문서 추출 상한 — 추출 텍스트가 이보다 길면 잘라냄(초대형 문서 폭주 방지).
INGEST_MAX_EXTRACT_CHARS = int(os.environ.get("WIKI_INGEST_MAX_EXTRACT_CHARS", "200000"))
# xlsx 시트당 직렬화 행 상한.
INGEST_XLSX_MAX_ROWS = int(os.environ.get("WIKI_INGEST_XLSX_MAX_ROWS", "200"))

# OCR — 스캔/이미지 PDF 및 이미지 파일 처리 (Apple Vision, 오프라인).
# 텍스트 추출이 빈약한 PDF에 자동 폴백. 바이너리가 없으면 조용히 건너뜀.
INGEST_OCR = os.environ.get("WIKI_INGEST_OCR", "1").lower() in ("1", "true", "yes")
_BUNDLE_ROOT = _PKG_DIR.parent.parent.parent  # openwebui/wiki-mcp/src/wiki_mcp → openwebui
_env_ocr_bin = os.environ.get("WIKI_INGEST_OCR_BIN", "").strip()
INGEST_OCR_BIN = (
    Path(_env_ocr_bin).expanduser()
    if _env_ocr_bin
    else _BUNDLE_ROOT / "ocr" / "vision-ocr"
)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "bge-m3")

# 원격 transport 설정 (stdio 기본 — backward compatible)
WIKI_MCP_TRANSPORT = os.environ.get("WIKI_MCP_TRANSPORT", "stdio")  # stdio | http
WIKI_MCP_HOST = os.environ.get("WIKI_MCP_HOST", "127.0.0.1")
WIKI_MCP_PORT = int(os.environ.get("WIKI_MCP_PORT", "8765"))
WIKI_MCP_PATH = os.environ.get("WIKI_MCP_PATH", "/mcp")
# @MX:WARN: [AUTO] empty API key = no auth — 127.0.0.1 외 바인딩 시 반드시 설정
# @MX:REASON: Bearer auth 없이 원격 노출되면 wiki 전체가 무인증 접근됨
WIKI_MCP_API_KEY = os.environ.get("WIKI_MCP_API_KEY", "")

# Embedding disable — when True, run in BM25-only mode (no Ollama/LM Studio needed)
EMBEDDING_DISABLED = os.environ.get("EMBEDDING_DISABLED", "").lower() in ("1", "true", "yes")

# Embedding backend — "ollama" (default, /api/embeddings) or "openai" (/v1/embeddings)
# "openai" supports LM Studio default API, OpenAI, Voyage, Cohere
EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "ollama").lower()

# API key for hosted OpenAI-compatible providers (LM Studio local needs none)
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
