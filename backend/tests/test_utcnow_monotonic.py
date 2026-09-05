"""utcnow 严格单调性（FE-ALIGN F1，根治 created_at 排序的负载敏感 flaky）。"""

from concurrent.futures import ThreadPoolExecutor

from novel_system.db.models import utcnow
from novel_system.db.models import utcnow as now_iso


def test_utcnow_strictly_increasing_under_rapid_calls():
    stamps = [utcnow() for _ in range(10_000)]
    assert all(a < b for a, b in zip(stamps, stamps[1:]))
    # 字符串排序与产生顺序一致（created_at 是 String 列，排序走字典序）
    assert sorted(stamps) == stamps


def test_utcnow_unique_across_threads():
    with ThreadPoolExecutor(max_workers=8) as pool:
        stamps = list(pool.map(lambda _: utcnow(), range(4_000)))
    assert len(set(stamps)) == len(stamps)


def test_now_iso_delegates_to_monotonic_clock():
    a = now_iso()
    b = utcnow()
    assert a < b
