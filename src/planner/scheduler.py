"""Алгоритм раскладки задач по календарю ресурса.

Модель (зафиксирована с пользователем):
- фазы задачи последовательны: СА -> Dev -> тест-буфер (5 раб. дней);
- на фазе задачи работает один человек: потребление <= 1 FTE в день,
  группа ведёт несколько задач параллельно в пределах дневной ёмкости;
- очередь: сначала задачи уже в разработке, затем по важности
  (levelOfImportance, меньше = важнее), затем по дедлайну и ключу;
- плановая дата завершения = ближайший день «Р» системы >= конца работ;
- раскладка жадная, ресурс группы списывается через CapacityLedger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .calendar_ru import add_workdays, next_workday
from .capacity import CapacityLedger
from .config import (
    COMPONENT_TO_TEAM,
    DIRECTION_TO_TEAM_ID,
    HOURS_PER_FTE_DAY,
    MAX_FTE_PER_TASK_PHASE,
    STATUSES_DEV_AHEAD,
    STATUSES_DEV_IN_PROGRESS,
    STATUSES_DONE,
    STATUSES_NOT_PLANNED,
    STATUSES_SA_AHEAD,
    STATUSES_TAIL_ONLY,
    SYSTEM_COMPONENT_TO_RELEASE_ROW,
    TEAM_BY_ID,
    Settings,
    Team,
)
from .release_plan import ReleasePlan
from .tracker import Issue


@dataclass
class PhasePlan:
    group: str
    hours: float
    start: date | None = None
    end: date | None = None
    days_used: int = 0


@dataclass
class PlanItem:
    issue: Issue
    team: Team | None
    systems: list[str]
    order: int = 0                    # позиция в очереди раскладки
    sa: PhasePlan | None = None
    dev: PhasePlan | None = None
    test_start: date | None = None    # начало тест-буфера (для Ганта)
    buffer_end: date | None = None
    release_date: date | None = None
    release_fallback: bool = False
    new_start: date | None = None
    new_end: date | None = None
    new_pdz: date | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class SkippedItem:
    issue: Issue
    reason: str


@dataclass
class PlanResult:
    planned: list[PlanItem]
    skipped: list[SkippedItem]
    unrecognized_statuses: dict[str, int]
    warnings: list[str]
    done_count: int = 0


def _team_of(issue: Issue) -> tuple[Team | None, bool]:
    """Команда задачи; второй элемент — найдена ли по фолбэку «Направление»."""
    for comp in issue.components:
        team = COMPONENT_TO_TEAM.get(comp.strip())
        if team:
            return team, False
    if issue.direction:
        team_id = DIRECTION_TO_TEAM_ID.get(issue.direction.strip())
        if team_id:
            return TEAM_BY_ID[team_id], True
    return None, False


def _team_has_capacity(team: Team, ledger: CapacityLedger) -> bool:
    """Есть ли в источнике ёмкость хотя бы по одной группе команды."""
    groups = [g for g in (team.sa_group, team.dev_group) if g]
    return any(ledger.has_group(g) for g in groups)


def _systems_of(issue: Issue) -> list[str]:
    rows = []
    for comp in issue.components:
        row = SYSTEM_COMPONENT_TO_RELEASE_ROW.get(comp.strip())
        if row:
            rows.append(row)
    return rows


def _sort_key(issue: Issue, in_progress_first: bool):
    in_progress = issue.status in STATUSES_DEV_IN_PROGRESS
    importance = issue.importance if issue.importance is not None else 10**9
    deadline = issue.deadline or date.max
    return (
        0 if (in_progress and in_progress_first) else 1,
        importance,
        deadline,
        issue.key,
    )


def _consume_phase(
    ledger: CapacityLedger,
    phase: PhasePlan,
    earliest: date,
    horizon: date,
) -> date | None:
    """Списывает часы фазы из группы день за днём; возвращает следующий
    рабочий день после конца фазы (для старта следующей) или None, если
    ёмкости не хватило до горизонта."""
    remaining_days = phase.hours / HOURS_PER_FTE_DAY
    day = next_workday(earliest)
    while remaining_days > 1e-9:
        if day > horizon:
            return None
        take = min(MAX_FTE_PER_TASK_PHASE, ledger.available(phase.group, day), remaining_days)
        if take > 1e-9:
            ledger.consume(phase.group, day, take)
            if phase.start is None:
                phase.start = day
            phase.end = day
            phase.days_used += 1
            remaining_days -= take
        day = next_workday(day + timedelta(days=1))
    return next_workday(phase.end + timedelta(days=1)) if phase.end else next_workday(earliest)


def build_plan(
    issues: list[Issue],
    ledger: CapacityLedger,
    releases: ReleasePlan,
    settings: Settings,
) -> PlanResult:
    planned: list[PlanItem] = []
    skipped: list[SkippedItem] = []
    unrecognized: dict[str, int] = {}
    warnings: list[str] = list(releases.warnings)

    known_statuses = (
        STATUSES_SA_AHEAD | STATUSES_DEV_AHEAD | STATUSES_DEV_IN_PROGRESS
        | STATUSES_TAIL_ONLY | STATUSES_NOT_PLANNED | STATUSES_DONE
    )

    # Команды без ресурса в источнике ёмкости — исключаем целиком.
    empty_teams: set[str] = set()
    if settings.exclude_teams_without_capacity:
        for team in settings.teams:
            if not _team_has_capacity(team, ledger):
                empty_teams.add(team.id)

    done_count = 0
    excluded_by_team: dict[str, int] = {}
    work_queue: list[Issue] = []
    for issue in issues:
        if issue.status in STATUSES_DONE:
            done_count += 1
            continue
        if issue.status in STATUSES_NOT_PLANNED:
            skipped.append(SkippedItem(issue, f"статус «{issue.status}» не планируется"))
            continue
        if issue.status not in known_statuses:
            unrecognized[issue.status] = unrecognized.get(issue.status, 0) + 1
            skipped.append(SkippedItem(issue, f"нераспознанный статус «{issue.status}»"))
            continue
        # Команда без ресурса в выгрузке — задача не расставляется.
        if empty_teams:
            team_early, _ = _team_of(issue)
            if team_early is not None and team_early.id in empty_teams:
                excluded_by_team[team_early.component] = excluded_by_team.get(team_early.component, 0) + 1
                skipped.append(SkippedItem(
                    issue, f"команда «{team_early.component}» без ресурса в выгрузке — исключена"))
                continue
        # Параметрические исключения (опционально, по умолчанию выключены).
        if issue.status in settings.exclude_statuses:
            skipped.append(SkippedItem(issue, f"исключён параметром --exclude-status «{issue.status}»"))
            continue
        if settings.exclude_in_progress and issue.status in STATUSES_DEV_IN_PROGRESS:
            skipped.append(SkippedItem(issue, f"исключён (--exclude-in-progress): «{issue.status}»"))
            continue
        if settings.exclude_tail and issue.status in STATUSES_TAIL_ONLY:
            skipped.append(SkippedItem(issue, f"исключён (--exclude-tail): «{issue.status}»"))
            continue
        # Пустая важность = задача не отобрана в порядок выполнения.
        # Исключения: уже идущая разработка (ресурс реально занят) и хвостовые
        # статусы (ресурс не потребляют, считается только релизное окно).
        if (
            issue.importance is None
            and issue.status not in STATUSES_DEV_IN_PROGRESS
            and issue.status not in STATUSES_TAIL_ONLY
        ):
            skipped.append(SkippedItem(issue, "не заполнена «Важность заявки» — не в очереди"))
            continue
        work_queue.append(issue)

    work_queue.sort(key=lambda i: _sort_key(i, settings.in_progress_first))

    for order, issue in enumerate(work_queue):
        team, via_direction = _team_of(issue)
        systems = _systems_of(issue)
        item = PlanItem(issue=issue, team=team, systems=systems, order=order)
        if via_direction:
            item.warnings.append(
                f"команда определена по полю «Направление» ({issue.direction}) — компонента «Команда: …» нет"
            )
        if issue.importance is None and issue.status in STATUSES_DEV_IN_PROGRESS:
            item.warnings.append("важность пуста, но задача уже в работе — оставлена в расчёте")

        sa_ahead = issue.status in STATUSES_SA_AHEAD
        dev_ahead = sa_ahead or issue.status in STATUSES_DEV_AHEAD | STATUSES_DEV_IN_PROGRESS
        tail_only = issue.status in STATUSES_TAIL_ONLY

        if not tail_only and team is None:
            skipped.append(SkippedItem(issue, "нет компонента «Команда: …» — не к кому планировать"))
            continue

        cursor = next_workday(settings.plan_start)

        if sa_ahead:
            if not issue.analyst_estimate:
                skipped.append(SkippedItem(issue, "СА-фаза впереди, а «ч/ч Аналитик» пуст"))
                continue
            if not team.sa_group:
                item.warnings.append(
                    f"у направления «{team.component}» нет группы СА в 1С — СА-фаза не спланирована"
                )
            else:
                item.sa = PhasePlan(group=team.sa_group, hours=float(issue.analyst_estimate))
                nxt = _consume_phase(ledger, item.sa, cursor, settings.horizon_end)
                if nxt is None:
                    item.warnings.append("ёмкости СА не хватило до горизонта планирования")
                    planned.append(item)
                    continue
                cursor = nxt

        if dev_ahead:
            if not issue.developer_estimate:
                skipped.append(SkippedItem(issue, "Dev-фаза впереди, а «ч/ч Разработчик» пуст"))
                continue
            item.dev = PhasePlan(group=team.dev_group, hours=float(issue.developer_estimate))
            nxt = _consume_phase(ledger, item.dev, cursor, settings.horizon_end)
            if nxt is None:
                item.warnings.append("ёмкости Dev не хватило до горизонта планирования")
                planned.append(item)
                continue
            cursor = nxt

        # Тест-буфер: 5 рабочих дней после последней рабочей фазы
        # (для tail-only задач — от даты запуска расчёта).
        item.test_start = next_workday(cursor)
        item.buffer_end = add_workdays(cursor, settings.test_buffer_workdays - 1)

        # Релизное окно: последний из ближайших «Р» всех систем задачи.
        if systems:
            candidates: list[tuple[date, bool]] = []
            for sysname in systems:
                rel, fb = releases.next_release(sysname, item.buffer_end)
                if rel:
                    candidates.append((rel, fb))
            if candidates:
                item.release_date = max(c[0] for c in candidates)
                item.release_fallback = any(c[1] for c in candidates)
        if item.release_date is None:
            item.release_date = item.buffer_end
            if not tail_only:
                item.warnings.append(
                    "нет компонента системы (1С УТ/БП/ЗУП/ДО/МДМ) — ПДЗ = конец работ без релизного окна"
                )

        first_phase_start = (
            (item.sa.start if item.sa and item.sa.start else None)
            or (item.dev.start if item.dev and item.dev.start else None)
        )
        # start не трогаем у задач, которые уже идут (есть фактический start в прошлом)
        if issue.start and issue.start <= settings.plan_start:
            item.new_start = issue.start
        else:
            item.new_start = first_phase_start
        item.new_end = item.buffer_end
        item.new_pdz = item.release_date
        planned.append(item)

    for component, count in sorted(excluded_by_team.items()):
        warnings.append(f"Команда «{component}» без ресурса в выгрузке — исключено задач: {count}")

    return PlanResult(
        planned=planned,
        skipped=skipped,
        unrecognized_statuses=unrecognized,
        warnings=warnings,
        done_count=done_count,
    )
