"""Производственный календарь: рабочие дни для мок-источника ёмкости.

Мок: сб/вс + фиксированный список праздников РФ 2026. В проде рабочие дни
приходят из 1С (РС ДанныеПроизводственногоКалендаря) вместе с ёмкостью,
и этот модуль используется только для тест-буфера и шагов по дням.
"""

from __future__ import annotations

from datetime import date, timedelta

from .config import RU_HOLIDAYS_2026


def is_workday(d: date) -> bool:
    return d.weekday() < 5 and d not in RU_HOLIDAYS_2026


def next_workday(d: date) -> date:
    """Ближайший рабочий день, не раньше d."""
    while not is_workday(d):
        d += timedelta(days=1)
    return d


def add_workdays(d: date, n: int) -> date:
    """Дата через n рабочих дней после d (n=0 -> сам d, выровненный вперёд)."""
    d = next_workday(d)
    for _ in range(n):
        d = next_workday(d + timedelta(days=1))
    return d


def workdays_between(start: date, end: date) -> list[date]:
    """Рабочие дни в интервале [start, end]."""
    days: list[date] = []
    d = start
    while d <= end:
        if is_workday(d):
            days.append(d)
        d += timedelta(days=1)
    return days
