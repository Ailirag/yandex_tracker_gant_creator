"""CLI планировщика.

Режимы:
  planner plan    — dry-run: расчёт и отчёты, Трекер только читается;
  planner backup  — снимок текущих дат очереди в файл (страховка перед apply);
  planner apply   — запись рассчитанных дат в Трекер из plan.json
                    (перед записью делает свежий бэкап изменяемых задач);
  planner restore — вернуть даты из бэкапа как было (в т.ч. очистить те,
                    что были пустыми); псевдоним: revert.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

from .capacity import CapacityLedger, FileCapacity, MockCapacity, OneCHttpCapacity
from .config import (
    EMPLOYEES_XLSX,
    ENV_FILE,
    OUT_DIR,
    QUEUE,
    RELEASE_PLAN_XLSX,
    RESERVE_DEFAULT,
    Settings,
)
from .release_plan import parse_release_plan
from .report_gantt import write_gantt_html
from .report import (
    console_summary,
    dump_json,
    issues_to_snapshot,
    write_backup,
    write_plan_json,
    write_xlsx,
)
from .scheduler import build_plan
from .shifts import compute_shifts, shifts_console
from .tracker import UNSET, open_client

DEFAULT_QUERY = f'"Queue": "{QUEUE}" "Resolution": unresolved()'
FULL_QUEUE_QUERY = f'"Queue": "{QUEUE}"'
LATEST_PLAN = OUT_DIR / "plan_latest.json"
LATEST_BACKUP = OUT_DIR / "backup_latest.json"


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(prompt).strip().lower() in ("yes", "y", "да")


def _parse_what_if(raw: str | None) -> dict[str, int]:
    """--what-if "ONE-123=5,ONE-456=1" -> {"ONE-123": 5, "ONE-456": 1}."""
    overrides: dict[str, int] = {}
    if not raw:
        return overrides
    for chunk in raw.split(","):
        key, _, value = chunk.strip().partition("=")
        if not key or not value:
            raise SystemExit(f"--what-if: ожидается КЛЮЧ=ВАЖНОСТЬ, получено «{chunk}»")
        overrides[key.strip()] = int(value)
    return overrides


def cmd_plan(args: argparse.Namespace) -> int:
    plan_start = date.fromisoformat(args.start) if args.start else date.today()
    sprint_anchor = date.fromisoformat(args.sprint_anchor) if args.sprint_anchor else plan_start
    exclude_statuses = {
        s.strip() for s in (args.exclude_status or "").split(",") if s.strip()
    }
    settings = Settings(
        plan_start=plan_start,
        exclude_in_progress=args.exclude_in_progress,
        exclude_tail=args.exclude_tail,
        exclude_statuses=exclude_statuses,
        exclude_teams_without_capacity=not args.keep_empty_teams,
    )
    print(f"Дата начала планирования: {plan_start.isoformat()}")
    print(f"Запрос к Трекеру: {args.query}")
    active = []
    if args.exclude_in_progress:
        active.append("в работе")
    if args.exclude_tail:
        active.append("на стадии завершения")
    if exclude_statuses:
        active.append(f"статусы: {', '.join(sorted(exclude_statuses))}")
    print("Исключения: " + ("; ".join(active) if active else "нет (все статусы включены)"))

    releases = parse_release_plan(RELEASE_PLAN_XLSX)

    if args.capacity == "onec":
        source = OneCHttpCapacity(
            base_url=args.onec_url,
            date_from=plan_start,
            date_to=settings.horizon_end,
        )
    elif args.capacity == "file":
        if not args.capacity_file:
            raise SystemExit("--capacity file требует --capacity-file <путь к выгрузке 1С>")
        source = FileCapacity.from_file(args.capacity_file)
    else:
        source = MockCapacity.from_xlsx(EMPLOYEES_XLSX)

    reserve = args.reserve / 100.0 if args.reserve > 1 else args.reserve
    ledger = CapacityLedger(source, reserve=reserve)
    capacity_note = f"{source.describe()}; резерв {reserve:.0%}"
    print(f"Источник ёмкости: {capacity_note}")

    client = open_client(ENV_FILE)
    try:
        issues = client.search_issues(args.query)
    finally:
        client.close()
    print(f"Задач получено из Трекера: {len(issues)}")

    what_if = _parse_what_if(args.what_if)
    if what_if:
        by_key = {i.key: i for i in issues}
        for key, importance in what_if.items():
            issue = by_key.get(key)
            if issue is None:
                raise SystemExit(f"--what-if: задача {key} не найдена в выборке")
            print(f"what-if: {key} важность {issue.importance} -> {importance}")
            issue.importance = importance

    result = build_plan(issues, ledger, releases, settings)

    # Базовый план для оценки последствий: явный --baseline, иначе последний.
    shifts = None
    baseline_path = Path(args.baseline) if args.baseline else (
        LATEST_PLAN if LATEST_PLAN.exists() and not args.no_baseline else None
    )
    if baseline_path:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        shifts, new_keys = compute_shifts(baseline, result)
        print(f"\n--- Сравнение с базовым планом ({baseline_path.name}) ---")
        print(shifts_console(shifts, new_keys))
        print()

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    xlsx_path = OUT_DIR / f"plan_{stamp}.xlsx"
    json_path = OUT_DIR / f"plan_{stamp}.json"
    backup_path = OUT_DIR / f"backup_{stamp}.json"
    gantt_path = OUT_DIR / f"gantt_{stamp}.html"
    write_xlsx(result, xlsx_path, capacity_note, shifts=shifts)
    write_plan_json(result, json_path)
    write_backup(result, backup_path)
    write_gantt_html(result, gantt_path, plan_start, capacity_note,
                     sprint_weeks=args.sprint_weeks, sprint_anchor=sprint_anchor)
    if what_if:
        print("Режим what-if: план НЕ сохранён как базовый (plan_latest.json не обновлён).")
    else:
        shutil.copyfile(json_path, LATEST_PLAN)

    print(console_summary(result, capacity_note))
    print(f"\nОтчёт:  {xlsx_path}")
    print(f"Гант:   {gantt_path}")
    print(f"План:   {json_path}")
    print(f"Бэкап:  {backup_path}")
    print("\nЭто dry-run: в Трекер ничего не записано. Запись: planner apply --plan <plan.json>")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Снимок текущих дат задач очереди — страховочная точка возврата."""
    client = open_client(ENV_FILE)
    try:
        issues = client.search_issues(args.query)
    finally:
        client.close()
    snapshot = issues_to_snapshot(issues)
    with_dates = sum(
        1 for s in snapshot
        if s["start"] or s["end"] or s["planned_completion"]
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = OUT_DIR / f"snapshot_{stamp}.json"
    dump_json(snapshot, path)
    dump_json(snapshot, LATEST_BACKUP)
    print(f"Снято задач: {len(snapshot)} (с проставленными датами: {with_dates}).")
    print(f"Бэкап: {path}")
    print(f"Он же: {LATEST_BACKUP} (последний)")
    print(f"\nВосстановление: planner restore --backup {path.name}")
    return 0


def _load_items(path: Path, keys: set[str] | None, limit: int | None = None) -> list[dict]:
    items = json.loads(path.read_text(encoding="utf-8"))
    if keys:
        items = [it for it in items if it["key"] in keys]
    if limit:
        items = items[:limit]
    return items


def cmd_apply(args: argparse.Namespace) -> int:
    keys = set(args.keys.split(",")) if args.keys else None
    items = _load_items(Path(args.plan), keys, args.limit)
    if not items:
        print("Нечего применять.")
        return 0
    affected = [it["key"] for it in items]
    print(f"К записи в Трекер: {len(items)} задач(и).")

    client = open_client(ENV_FILE)
    try:
        # Свежий бэкап РОВНО изменяемых задач — состояние на момент записи,
        # а не на момент расчёта плана. Без него apply не выполняется.
        if not args.no_backup:
            print("Снимаю бэкап изменяемых задач перед записью…")
            current = client.fetch_current_by_keys(affected)
            stamp = datetime.now().strftime("%Y%m%d_%H%M")
            backup_path = OUT_DIR / f"preapply_{stamp}.json"
            dump_json(issues_to_snapshot(current), backup_path)
            dump_json(issues_to_snapshot(current), LATEST_BACKUP)
            print(f"Бэкап: {backup_path}")
            print(f"Откат при проблемах: planner restore --backup {backup_path.name}")

        if not _confirm("Подтвердите запись (yes/нет): ", args.yes):
            print("Отменено (бэкап сохранён).")
            return 1

        ok, failed = 0, 0
        for it in items:
            try:
                # None в плане = не рассчитано -> UNSET (поле не трогаем).
                client.update_issue_dates(
                    it["key"],
                    start=date.fromisoformat(it["new_start"]) if it.get("new_start") else UNSET,
                    end=date.fromisoformat(it["new_end"]) if it.get("new_end") else UNSET,
                    planned_completion=date.fromisoformat(it["new_planned_completion"])
                    if it.get("new_planned_completion") else UNSET,
                )
                ok += 1
                print(f"  {it['key']}: записано")
            except Exception as exc:  # noqa: BLE001 — продолжаем остальные задачи
                failed += 1
                print(f"  {it['key']}: ОШИБКА {exc}")
    finally:
        client.close()
    print(f"Готово: {ok} записано, {failed} с ошибками.")
    return 0 if failed == 0 else 2


def cmd_restore(args: argparse.Namespace) -> int:
    """Вернуть даты как в бэкапе: пустые в бэкапе поля очищаются в Трекере."""
    keys = set(args.keys.split(",")) if args.keys else None
    items = _load_items(Path(args.backup), keys)
    if not items:
        print("В бэкапе нет подходящих задач.")
        return 0
    print(f"К восстановлению: {len(items)} задач(и) из {Path(args.backup).name}.")
    if not _confirm("Подтвердите восстановление (yes/нет): ", args.yes):
        print("Отменено.")
        return 1
    client = open_client(ENV_FILE)
    ok, failed = 0, 0
    try:
        for it in items:
            try:
                # None -> очистить поле (вернуть в исходно пустое состояние).
                client.update_issue_dates(
                    it["key"],
                    start=date.fromisoformat(it["start"]) if it.get("start") else None,
                    end=date.fromisoformat(it["end"]) if it.get("end") else None,
                    planned_completion=date.fromisoformat(it["planned_completion"])
                    if it.get("planned_completion") else None,
                )
                ok += 1
                print(f"  {it['key']}: восстановлено")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  {it['key']}: ОШИБКА {exc}")
    finally:
        client.close()
    print(f"Готово: {ok} восстановлено, {failed} с ошибками.")
    return 0 if failed == 0 else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="planner", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="рассчитать план (dry-run)")
    p_plan.add_argument("--start", help="дата начала раскладки YYYY-MM-DD (по умолчанию сегодня)")
    p_plan.add_argument("--query", default=DEFAULT_QUERY,
                        help=f"запрос к Трекеру (по умолчанию: {DEFAULT_QUERY})")
    p_plan.add_argument("--exclude-in-progress", action="store_true",
                        help="исключить задачи в работе (В разработке, Устранение замечаний)")
    p_plan.add_argument("--exclude-tail", action="store_true",
                        help="исключить задачи на стадии завершения (тест/ревью/релиз)")
    p_plan.add_argument("--exclude-status",
                        help='исключить произвольные статусы по имени: "Пауза,Отложено"')
    p_plan.add_argument("--keep-empty-teams", action="store_true",
                        help="НЕ исключать команды, для которых нет ресурса в источнике ёмкости")
    p_plan.add_argument("--capacity", choices=("mock", "onec", "file"), default="mock",
                        help="источник ёмкости: mock (по умолчанию), file (выгрузка 1С), onec (HTTP-сервис)")
    p_plan.add_argument("--capacity-file", help="путь к выгрузке 1С (для --capacity file)")
    p_plan.add_argument("--reserve", type=float, default=RESERVE_DEFAULT,
                        help="резерв ёмкости под влёты, доля 0..1 или %% (по умолчанию 0.25); "
                             "0 — без резерва (когда заведён событиями в 1С)")
    p_plan.add_argument("--sprint-weeks", type=int, default=2,
                        help="длина спринта в неделях для сетки на Ганте (по умолчанию 2)")
    p_plan.add_argument("--sprint-anchor",
                        help="дата начала любого спринта YYYY-MM-DD (по умолчанию — дата плана)")
    p_plan.add_argument("--onec-url", default="http://localhost/sprinthelper/hs/planner",
                        help="базовый URL HTTP-сервиса 1С (для --capacity onec)")
    p_plan.add_argument("--baseline", help="plan_*.json для сравнения (по умолчанию plan_latest.json)")
    p_plan.add_argument("--no-baseline", action="store_true", help="не сравнивать с прошлым планом")
    p_plan.add_argument("--what-if", help='виртуальные важности: "ONE-123=5,ONE-456=1" (план не сохраняется как базовый)')
    p_plan.set_defaults(func=cmd_plan)

    p_backup = sub.add_parser("backup", help="снять снимок текущих дат очереди")
    p_backup.add_argument("--query", default=FULL_QUEUE_QUERY,
                          help="запрос к Трекеру (по умолчанию вся очередь)")
    p_backup.set_defaults(func=cmd_backup)

    p_apply = sub.add_parser("apply", help="записать план в Трекер")
    p_apply.add_argument("--plan", required=True, help="путь к plan_*.json")
    p_apply.add_argument("--keys", help="только эти ключи, через запятую")
    p_apply.add_argument("--limit", type=int, help="не более N задач")
    p_apply.add_argument("--yes", action="store_true", help="без интерактивного подтверждения")
    p_apply.add_argument("--no-backup", action="store_true",
                         help="не делать бэкап перед записью (не рекомендуется)")
    p_apply.set_defaults(func=cmd_apply)

    # restore + псевдоним revert для обратной совместимости
    for name, help_text in (("restore", "вернуть даты из бэкапа как было"),
                            ("revert", "псевдоним restore")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--backup", required=True, help="путь к snapshot_/preapply_/backup_*.json")
        p.add_argument("--keys", help="только эти ключи, через запятую")
        p.add_argument("--yes", action="store_true")
        p.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
