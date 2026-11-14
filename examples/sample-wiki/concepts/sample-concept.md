---
id: sample-concept
title: "샘플 개념 — 원자 노트 패턴"
source_url: ""
tags: [sample, pattern]
created_at: "2026-07-09"
last_updated: "2026-07-09"
---

# 원자 노트 패턴

이 노트는 위키에서 하나의 "원자 개념"을 어떻게 표현하는지 보여주는 예시입니다. 원자 노트는 한 가지 아이디어만 담고, 명확한 `id`를 가지며, 다른 노트로 `[[id]]` 링크로 연결됩니다.

## Frontmatter 규칙

모든 노트는 YAML frontmatter로 시작합니다.

- `id`: kebab-case, 소문자 영문/숫자/하이픈 (`^[a-z0-9][a-z0-9-]*$`). 한글은 ID에 쓸 수 없습니다.
- `title`: 사람이 읽기 좋은 제목 (한국어 가능).
- `tags`: 관련 키워드 배열 — `search_wiki`의 `tag_filter`로 활용.
- `created_at` / `last_updated`: `YYYY-MM-DD` 형식. `update_note` 도구가 `last_updated`를 자동 갱신.

## 링크 규칙

본문이나 `## 관련 노트` 섹션에서 다른 노트를 참조합니다.

- `[[welcome]]` — id로 직접 연결
- `[[welcome|환영 노트]]` — 표시 텍스트를 별도로 지정

`add_link` 도구는 단방향 링크만 추가하므로, 양방향으로 묶으려면 양쪽 모두에서 호출해야 합니다. 예를 들어 이 노트는 [[welcome|환영 노트]]를 가리키지만, 환영 노트도 이쪽을 가리키도록 링크가 설정되어 있습니다.

## 검색 동작

`search_wiki("원자 노트")`를 호출하면 BM25 + dense(임베딩 활성화 시) RRF 퓨전으로 이 노트가 상위에 랭크됩니다. `tag_filter="pattern"`으로 좁히는 것도 가능합니다.

## 관련 노트

- [[welcome|환영 노트]]
- [[another-concept|또 다른 개념]]
- [[index|허브로 돌아가기]]
