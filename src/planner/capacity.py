"""Источники ёмкости (FTE по дням) для групп «роль × направление».

Два источника:
- OneCHttpCapacity — боевой: HTTP-сервис 1С отдаёт остатки РН
  «ДоступностьРесурса» по группам (резервы и отпуска уже учтены документами);
- MockCapacity — до появления сервиса: численность групп из
  «Сотрудники в иерархии.xlsx» × коэффициент резерва × рабочие дни.

Контракт: capacity(group, day) -> FTE (float >= 0). Планировщик сам
уменьшает остаток при раскладке через CapacityLedger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

import httpx
import openpyxl

from .calendar_ru import is_workday
from .config import RESERVE_COEFF_DEV, RESERVE_COEFF_SA, TEAMS


class CapacitySource(Protocol):
    def fte(self, group: str, day: date) -> float: ...
    def known_groups(self) -> set[str]: ...
    def describe(self) -> str: ...


# --- Мок из xlsx ---------------------------------------------------------

_SA_GROUPS = {t.sa_group for t in TEAMS if t.sa_group}
_DEV_GROUPS = {t.dev_group for t in TEAMS if t.dev_group}


@dataclass
class MockCapacity:
    """Ёмкость = активная численность группы × резервный коэффициент.

    Сотрудник учитывается, если не помечен «Исключить из ресурса» и период
    работы (ДатаНачалаРаботы/ДатаОкончанияРаботы) покрывает день. Отпуска мок
    не видит — это осознанная погрешность до подключения HTTP-сервиса 1С.
    """
    headcount: dict[str, list[tuple[date | None, date | None]]]

    @classmethod
    def from_xlsx(cls, xlsx_path) -> "MockCapacity":
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        wb.close()

        def parse_d(v) -> date | None:
            if v is None or str(v).startswith("<"):
                return None
            if isinstance(v, date):
                return v
            try:
                dd, mm, yy = str(v).strip().split(".")
                return date(int(yy), int(mm), int(dd))
            except Exception:
                return None

        headcount: dict[str, list[tuple[date | None, date | None]]] = {}
        for row in rows:
            parent, ref, _, deleted, is_group, _, name = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
            excluded = row[13] if len(row) > 13 else None
            if ref is None or str(is_group) == "Да":
                continue
            if str(deleted) == "Да" or str(excluded) == "Да":
                continue
            start = parse_d(row[10] if len(row) > 10 else None)
            end = parse_d(row[12] if len(row) > 12 else None)
            headcount.setdefault(str(parent).strip(), []).append((start, end))
        return cls(headcount=headcount)

    def _match_group(self, group: str) -> str | None:
        """Имена групп в 1С и xlsx могут отличаться суффиксом «(1С)» и пробелами."""
        norm = lambda s: s.replace("(1С)", "").replace(" ", "").lower()
        for known in self.headcount:
            if norm(known) == norm(group):
                return known
        return None

    def fte(self, group: str, day: date) -> float:
        if not is_workday(day):
            return 0.0
        known = self._match_group(group)
        if known is None:
            return 0.0
        active = 0
        for start, end in self.headcount[known]:
            if start and day < start:
                continue
            if end and day > end:
                continue
            active += 1
        coeff = RESERVE_COEFF_SA if group in _SA_GROUPS else RESERVE_COEFF_DEV
        return active * coeff

    def known_groups(self) -> set[str]:
        return {g for g in (_SA_GROUPS | _DEV_GROUPS) if self._match_group(g)}

    def describe(self) -> str:
        return (
            "МОК: численность групп из xlsx × резерв "
            f"(Dev {RESERVE_COEFF_DEV:.0%}, СА {RESERVE_COEFF_SA:.0%}); отпуска не учтены"
        )


# --- HTTP-сервис 1С ------------------------------------------------------

@dataclass
class OneCHttpCapacity:
    """Клиент будущего HTTP-сервиса 1С (см. onec/README.md).

    GET {base_url}/capacity?from=YYYY-MM-DD&to=YYYY-MM-DD ->
    {"groups": {"<Группа>": {"YYYY-MM-DD": 3.5, ...}, ...}}
    """
    base_url: str
    date_from: date
    date_to: date
    _cache: dict[str, dict[str, float]] = field(default_factory=dict)

    def _load(self) -> None:
        if self._cache:
            return
        resp = httpx.get(
            f"{self.base_url.rstrip('/')}/capacity",
            params={"from": self.date_from.isoformat(), "to": self.date_to.isoformat()},
            timeout=120,
        )
        resp.raise_for_status()
        self._cache = resp.json()["groups"]

    def fte(self, group: str, day: date) -> float:
        self._load()
        return float(self._cache.get(group, {}).get(day.isoformat(), 0.0))

    def known_groups(self) -> set[str]:
        self._load()
        return set(self._cache)

    def describe(self) -> str:
        return f"HTTP-сервис 1С: {self.base_url} (остатки РН ДоступностьРесурса)"


# --- Леджер остатков для раскладки ---------------------------------------

class CapacityLedger:
    """Изменяемые остатки ёмкости поверх источника (жадная раскладка)."""

    def __init__(self, source: CapacitySource):
        self._source = source
        self._used: dict[tuple[str, date], float] = {}

    def available(self, group: str, day: date) -> float:
        base = self._source.fte(group, day)
        return max(0.0, base - self._used.get((group, day), 0.0))

    def consume(self, group: str, day: date, fte: float) -> None:
        self._used[(group, day)] = self._used.get((group, day), 0.0) + fte

    def utilization(self) -> dict[tuple[str, date], float]:
        return dict(self._used)
