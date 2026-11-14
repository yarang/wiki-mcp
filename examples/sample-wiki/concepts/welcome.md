---
id: welcome
title: "환영 노트 — 설치 및 빠른 시작"
source_url: ""
tags: [welcome, setup]
created_at: "2026-07-09"
last_updated: "2026-07-09"
---

# 환영합니다

`wiki-mcp`는 한국어 원자 노트 위키를 MCP(Model Context Protocol) 클라이언트(Open-WebUI, Claude Code, Cursor, Cline 등)에 노출하는 서버입니다. BM25 + dense embedding 하이브리드 검색, `[[id]]` 링크 그래프 분석, 자연어 CRUD 도구를 함께 제공합니다.

## 설치 (요약)

guardian 통합 번들에서는 `wiki-mcp`를 설치 스크립트가 wheel로 자동 설치합니다
(`~/.guardian-llm/venv`에 들어갑니다). 단독으로 설치할 때는 빌드된 wheel을 씁니다
(예: `pip install wiki_mcp-0.2.4-py3-none-any.whl`).

## 첫 실행

`WIKI_PATH`를 비우면 이 샘플 위키가 기본값으로 사용됩니다.

```bash
wiki-mcp          # stdio 모드 (기본) — MCP 클라이언트가 연결
```

임베딩 백엔드 없이도 BM25-only로 동작합니다.

```bash
EMBEDDING_DISABLED=1 wiki-mcp
```

## 내 위키로 전환

```bash
WIKI_PATH=/path/to/my-wiki wiki-mcp
```

## 다음 단계

- [[sample-concept|샘플 개념]] — 원자 노트의 frontmatter/링크 패턴
- [[another-concept|또 다른 개념]] — 링크 그래프가 어떻게 형성되는지 확인
- 설정 옵션은 [`docs/USAGE.md`의 "환경 변수"](../../../../docs/USAGE.md#환경-변수) 절에서 확인

## 관련 노트

- [[sample-concept|샘플 개념]]
- [[another-concept|또 다른 개념]]
- [[index|허브로 돌아가기]]
