---
id: another-concept
title: "또 다른 개념 — 링크 그래프 예시"
source_url: ""
tags: [sample, graph]
created_at: "2026-07-09"
last_updated: "2026-07-09"
---

# 링크 그래프의 한 점

이 노트는 위키의 `[[id]]` 양방향 링크 그래프가 어떻게 형성되는지 보여주는 두 번째 예시입니다. 여러 노트가 서로를 가리키면 `find_related` 도구의 BFS 탐색이 의미 있는 결과를 만듭니다.

## 연결 확인

`find_related("welcome", depth=2)`를 호출하면, `welcome` → 이 노트 → [[sample-concept|샘플 개념]] 경로로 도달합니다. 깊이를 늘리면 더 먼 이웃까지 탐색합니다.

## In-degree / Hub 점수

`get_wiki_stats` 도구는 in-degree(이 노트를 가리키는 링크 수)를 기준으로 상위 hub 노트를 나열합니다. 이 샘플 위키에서는 [[welcome|환영 노트]]와 [[sample-concept|샘플 개념]]이 상위 hub에 해당합니다.

## Broken link 점검

`find_broken_links` 도구는 존재하지 않는 `[[id]]`를 참조하는 노트를 찾습니다. 이 스켈레톤에는 의도적으로 broken link를 두지 않았지만, 실제 위키에서는 노트 삭제 후 종종 발생합니다.

## 관련 노트

- [[welcome|환영 노트]]
- [[sample-concept|샘플 개념]]
- [[index|허브로 돌아가기]]
