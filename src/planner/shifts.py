"""Сравнение нового плана с базовым: кто сместился и из-за кого.

Базовый план — plan_*.json предыдущего прогона. Для каждой задачи, чей
расчётный end изменился, считается смещение в рабочих днях и подбираются
кандидаты-виновники: задачи той же команды, вставшие в очередь раньше неё,
которых в базовом плане не было (получили важность, созданы, переопределены
через --what-if).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .calendar_ru import workdays_between
from .scheduler import PlanResult


@dataclass
class Shift:
    key: str
    summary: str
    team: str
    old_end: date | None
    new_end: date | None
    delta_workdays: int          # >0 — задача уехала позже
    old_pdz: date | None
    new_pdz: date | None
    suspects: str                # вероятные причины смещения


def _signed_workdays(old: date, new: date) -> int:
    if new > old:
        return len(workdays_between(old + timedelta(days=1), new))
    if new < old:
        return -len(workdays_between(new + timedelta(days=1), old))
    return 0


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def compute_shifts(baseline: list[dict], result: PlanResult) -> tuple[list[Shift], list[str]]:
    """Возвращает (смещения, новые_в_плане_ключи)."""
    base_by_key = {b["key"]: b for b in baseline}
    new_keys = [it.issue.key for it in result.planned if it.issue.key not in base_by_key]
    newcomers_by_team: dict[str, list] = {}
    for it in result.planned:
        if it.issue.key in base_by_key or it.team is None:
            continue
        newcomers_by_team.setdefault(it.team.id, []).append(it)

    shifts: list[Shift] = []
    for it in result.planned:
        base = base_by_key.get(it.issue.key)
        if base is None:
            continue
        old_end = _d(base.get("new_end"))
        if old_end is None or it.new_end is None:
            continue
        delta = _signed_workdays(old_end, it.new_end)
        if delta == 0:
            continue

        suspects = ""
        if delta > 0 and it.team is not None:
            movers = [
                n for n in newcomers_by_team.get(it.team.id, [])
                if n.order < it.order
            ]
            movers.sort(key=lambda n: n.order)
            parts = []
            for n in movers[:3]:
                hours = sum(p.hours for p in (n.sa, n.dev) if p)
                parts.append(f"{n.issue.key} (+{hours:g} ч)")
            if len(movers) > 3:
                parts.append(f"и ещё {len(movers) - 3}")
            suspects = "выше в очереди встали: " + ", ".join(parts) if parts else ""
        if delta > 0 and not suspects:
            suspects = "изменение ёмкости/оценок/порядка (новых задач впереди нет)"

        shifts.append(Shift(
            key=it.issue.key,
            summary=it.issue.summary,
            team=it.team.component if it.team else "",
            old_end=old_end,
            new_end=it.new_end,
            delta_workdays=delta,
            old_pdz=_d(base.get("new_planned_completion")),
            new_pdz=it.new_pdz,
            suspects=suspects,
        ))

    shifts.sort(key=lambda s: -abs(s.delta_workdays))
    return shifts, new_keys


def shifts_console(shifts: list[Shift], new_keys: list[str]) -> str:
    if not shifts and not new_keys:
        return "Смещений относительно базового плана нет."
    lines = []
    if new_keys:
        lines.append(f"Новых задач в плане: {len(new_keys)} ({', '.join(new_keys[:10])}"
                     + (", …)" if len(new_keys) > 10 else ")"))
    later = [s for s in shifts if s.delta_workdays > 0]
    earlier = [s for s in shifts if s.delta_workdays < 0]
    if later:
        lines.append(f"Сдвинуто ПОЗЖЕ: {len(later)} задач(и), максимум +{later[0].delta_workdays} раб. дн.")
        for s in later[:5]:
            lines.append(f"  {s.key}: {s.old_end} -> {s.new_end} (+{s.delta_workdays} рд). {s.suspects}")
    if earlier:
        lines.append(f"Сдвинуто раньше: {len(earlier)} задач(и).")
    return "\n".join(lines)
