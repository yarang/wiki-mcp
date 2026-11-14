"""KB 도메인 예외.

KBError는 고정 code를 가진다 — CLI 종료 코드·JSON 계약(docs/cli-contract.md)의
error.code와 같은 값을 쓴다. 민감 폴더 경고는 예외가 아니라 "등록하지 않고
warning dict를 돌려주는" 반환 값이다(add_source 참고).
"""

from __future__ import annotations


class KBError(Exception):
    """KB 입력·상태 오류. code는 CLI JSON 계약의 error.code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
