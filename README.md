# wiki-mcp (소스)

LLM Wiki를 [MCP](https://modelcontextprotocol.io/) tool로 노출하는 서버입니다. 현재 버전 **0.2.4**.
이 디렉토리는 저장소에 담긴 wiki-mcp 소스입니다. guardian 번들에서는 이 소스로 **wheel을 만들어**
(`pip wheel`) 대상 Mac의 `~/.guardian-llm/venv`에 설치합니다. editable(`-e`) 설치는 쓰지 않습니다.

## 설치·설정·사용법

현재 기준 문서는 **`guardian/README.md`**입니다 (번들 만들기·설치·운영).
번들 루트의 `INSTALL.md`는 guardian 통합 이전의 **구 흐름(단독 번들)** 문서이므로 참고용으로만 보세요.

| 내용 | 위치 |
| --- | --- |
| 번들 만들기·설치·운영 (현행) | `guardian/README.md` |
| 구 흐름 설치 (참고용) | 번들 루트 `INSTALL.md` |
| 노출 도구 목록 (15개) | `src/wiki_mcp/server.py` |
| 환경 변수 | `src/wiki_mcp/config.py` + `guardian/wiki.env.example` |

## 소스 구성

```text
src/wiki_mcp/
├── server.py      # FastMCP 서버 — MCP tool 정의/등록 (15개)
├── indexer.py     # 인덱싱 + 하이브리드 검색 (BM25 / 임베딩)
├── autoindex.py   # mcpo 프로세스 안에서 노트 변경 감지 → 자동 재인덱싱(임베딩)
├── graph.py       # [[id]] 링크 그래프
├── writer.py      # 노트 생성·수정·삭제 (검증·백업·템플릿)
├── ingest.py      # raw 원문 → 노트 변환 (LLM, 아웃라인→작성 청킹)
├── extract.py     # 문서 텍스트 추출 (PDF/docx/pptx/xlsx/html) + OCR 연동
├── watch.py       # raw 폴더 감시 — 원문 자동 변환 (재인덱싱은 autoindex가 담당)
├── config.py      # 환경 변수 / 경로 해석
├── auth.py        # HTTP 모드 Bearer 인증
├── __main__.py    # `python -m wiki_mcp` 실행 지원
└── __init__.py

examples/sample-wiki/   # WIKI_PATH 미지정 시 폴백용 예제 (번들에서는 미사용)
```

경로 해석: guardian 설치에서는 `~/llm-wiki/.env`의 절대경로를 사용합니다
(LaunchAgent는 셸 프로필을 읽지 않습니다). `.env`가 없으면 번들 루트 기준 기본값으로 해석됩니다.
