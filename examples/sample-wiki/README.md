# 샘플 위키 스켈레톤

이 디렉토리는 `wiki-mcp` 첫 실행 및 데모용으로 제공되는 최소 위키 스켈레톤입니다.

## 용도

- `WIKI_PATH`를 지정하지 않고 `wiki-mcp`를 실행하면 이 스켈레톤이 자동으로 사용됩니다.
- 노트 개수가 적어도 15개 도구(`search_wiki`, `read_note`, `create_note`, …)가 어떻게 동작하는지 바로 확인할 수 있습니다.
- 실제 위키로 교체하려면 `WIKI_PATH=/path/to/your/wiki wiki-mcp`로 실행하세요.

## 구조

```
sample-wiki/
├── README.md                      # 이 파일
├── index.md                       # 허브 노트 (id: index)
└── concepts/
    ├── welcome.md                 # 설치/사용 가이드 (id: welcome)
    ├── sample-concept.md          # 원자 노트 예시 (id: sample-concept)
    └── another-concept.md         # 두 번째 개념 (id: another-concept)
```

## 노트 규칙 (요약)

- 각 노트는 YAML frontmatter로 시작하며 `id`, `title`, `tags`, `created_at`, `last_updated`를 포함합니다.
- `id`는 kebab-case 소문자 영문/숫자/하이픈만 허용합니다 (예: `sample-concept`).
- 노트 간 연결은 `[[id]]` 또는 `[[id|표시 텍스트]]` 형식의 위키링크를 사용합니다.
- `## 관련 노트` 섹션에 단방향 링크를 정리하는 것이 관례입니다.

전체 규칙은 [`docs/USAGE.md`의 "아키텍처 요약"](../../../docs/USAGE.md#아키텍처-요약) 절을 참고하세요.
