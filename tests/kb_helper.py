"""KB 테스트 공용 헬퍼 — 스캔 시계 주입, 초안 고정 시계 등."""

from __future__ import annotations

import time
from datetime import datetime, timezone

# 초안 저장·승인 테스트에 주입하는 고정 시계 — UTC 정오(2026-11-15).
# 초안 id 는 d-20261115-120000-<token> 형태로 결정된다.
DRAFT_NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)


def future_now(seconds: float = 60.0):
    """스캔용 시계 — 지금보다 미래를 보게 해 방금 쓴 파일이 unstable 로
    건너뛰어지지 않게 한다(파일 mtime 이 '스캔 시각'보다 충분히 과거가 된다)."""
    return lambda: time.time() + seconds
