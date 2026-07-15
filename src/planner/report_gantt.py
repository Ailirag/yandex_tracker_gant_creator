"""HTML-диаграмма Ганта по командам (самодостаточный файл, без внешних зависимостей).

Рендер поверх рассчитанного плана: свимлейны по командам, задача — полоска с
сегментами фаз (СА / Dev / тест-буфер / ожидание релиза), маркеры релиза и
дедлайна, линия «сегодня». Ничего не вычисляет заново — только отображает
данные PlanResult.
"""

from __future__ import annotations

import html
from datetime import date, timedelta

from .scheduler import PlanItem, PlanResult

PX_PER_DAY = 9
ROW_H = 26
LABEL_W = 340

PHASE_COLORS = {
    "СА": "#4C78A8",
    "Dev": "#59A14F",
    "Тест": "#E8A838",
    "Ожидание": "#B6ADA5",
}


def _seg(item: PlanItem):
    """Сегменты фаз задачи: (класс, подпись, дата_нач, дата_кон)."""
    out = []
    if item.sa and item.sa.start and item.sa.end:
        out.append(("СА", f"СА {item.sa.hours:g} ч", item.sa.start, item.sa.end))
    if item.dev and item.dev.start and item.dev.end:
        out.append(("Dev", f"Dev {item.dev.hours:g} ч", item.dev.start, item.dev.end))
    if item.test_start and item.buffer_end and item.buffer_end >= item.test_start:
        out.append(("Тест", "тест-буфер", item.test_start, item.buffer_end))
    if item.release_date and item.buffer_end and item.release_date > item.buffer_end:
        out.append(("Ожидание", "ожидание релиза", item.buffer_end + timedelta(days=1), item.release_date))
    return out


def _axis_range(result: PlanResult, plan_start: date) -> tuple[date, date]:
    """Ось = от даты плана до последнего запланированного события.

    Дедлайны и старты в прошлом НЕ раздвигают шкалу (иначе редкий выброс
    сжимает всю реальную работу в узкую полосу). Такие даты потом обрезаются
    к краям оси при отрисовке.
    """
    ends = [plan_start]
    for it in result.planned:
        for _, _, _, e in _seg(it):
            ends.append(e)
        if it.release_date:
            ends.append(it.release_date)
        if it.new_end:
            ends.append(it.new_end)
    date_to = max(ends)
    return plan_start, date_to


def write_gantt_html(result: PlanResult, out_path, plan_start: date, capacity_note: str) -> None:
    date_from, date_to = _axis_range(result, plan_start)
    date_from -= timedelta(days=date_from.weekday())          # к понедельнику
    total_days = (date_to - date_from).days + 3
    total_px = total_days * PX_PER_DAY

    def x(d: date) -> int:
        return (d - date_from).days * PX_PER_DAY

    def clamp(d: date) -> date:
        return min(max(d, date_from), date_to)

    def in_range(d: date) -> bool:
        return date_from <= d <= date_to

    # шкала месяцев
    month_ticks = []
    d = date_from.replace(day=1)
    while d <= date_to:
        month_ticks.append((x(d), d.strftime("%m.%Y")))
        d = (d.replace(day=28) + timedelta(days=7)).replace(day=1)

    # группировка по командам (порядок как в конфиге), затем без команды
    from .config import TEAMS
    order = {t.component: i for i, t in enumerate(TEAMS)}
    groups: dict[str, list[PlanItem]] = {}
    for it in result.planned:
        key = it.team.component if it.team else "Без команды"
        groups.setdefault(key, []).append(it)
    for lst in groups.values():
        lst.sort(key=lambda i: i.order)
    ordered_groups = sorted(groups, key=lambda k: order.get(k, 999))

    rows_html = []
    for gname in ordered_groups:
        items = groups[gname]
        rows_html.append(
            f'<div class="team"><div class="team-name">{html.escape(gname)} '
            f'<span class="team-count">· {len(items)}</span></div>'
            f'<div class="team-track"></div></div>'
        )
        for it in items:
            segs = _seg(it)
            bars = []
            for cls, label, s, e in segs:
                cs, ce = clamp(s), clamp(e)
                left = x(cs)
                width = max(PX_PER_DAY, ((ce - cs).days + 1) * PX_PER_DAY)
                bars.append(
                    f'<div class="seg {cls}" style="left:{left}px;width:{width}px" '
                    f'title="{html.escape(label)}: {s.isoformat()}–{e.isoformat()}"></div>'
                )
            # маркер релиза
            if it.release_date and in_range(it.release_date):
                bars.append(f'<div class="mark rel" style="left:{x(it.release_date)}px" '
                            f'title="Релиз: {it.release_date.isoformat()}"></div>')
            # дедлайн (красный, если срыв); вне оси не рисуем
            dl = it.issue.deadline
            breach = dl and it.new_end and it.new_end > dl
            if dl and in_range(dl):
                cls = "dl breach" if breach else "dl"
                bars.append(f'<div class="mark {cls}" style="left:{x(dl)}px" '
                            f'title="Дедлайн: {dl.isoformat()}"></div>')

            imp = it.issue.importance if it.issue.importance is not None else "—"
            warn = " ⚠" if it.warnings else ""
            tip = _tooltip(it)
            label_cls = "label breach" if breach else "label"
            rows_html.append(
                f'<div class="row" data-tip="{html.escape(tip)}">'
                f'<div class="{label_cls}"><span class="imp">{imp}</span> '
                f'<a href="https://tracker.yandex.ru/{it.issue.key}" target="_blank">{it.issue.key}</a> '
                f'<span class="nm">{html.escape(it.issue.summary[:60])}{warn}</span></div>'
                f'<div class="track">{"".join(bars)}</div></div>'
            )

    axis = "".join(
        f'<div class="mtick" style="left:{px}px">{lbl}</div>' for px, lbl in month_ticks
    )
    today_x = x(plan_start)
    planned_n = len(result.planned)

    doc = _TEMPLATE.format(
        total_px=total_px,
        label_w=LABEL_W,
        row_h=ROW_H,
        row_h2=ROW_H - 4,
        seg_h=ROW_H - 10,
        wd_px=PX_PER_DAY * 5,
        wk_px=PX_PER_DAY * 7,
        axis=axis,
        rows="".join(rows_html),
        today_x=today_x,
        planned_n=planned_n,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        capacity=html.escape(capacity_note),
        generated=plan_start.isoformat(),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")


def _tooltip(it: PlanItem) -> str:
    lines = [f"{it.issue.key}: {it.issue.summary}",
             f"Статус: {it.issue.status} · Важность: {it.issue.importance}"]
    if it.sa and it.sa.start:
        lines.append(f"СА: {it.sa.start}–{it.sa.end} ({it.sa.hours:g} ч)")
    if it.dev and it.dev.start:
        lines.append(f"Dev: {it.dev.start}–{it.dev.end} ({it.dev.hours:g} ч)")
    if it.test_start and it.buffer_end:
        lines.append(f"Тест: {it.test_start}–{it.buffer_end}")
    if it.release_date:
        fb = " (фолбэк)" if it.release_fallback else ""
        lines.append(f"Релиз/ПДЗ: {it.release_date}{fb}")
    if it.issue.deadline:
        lines.append(f"Дедлайн: {it.issue.deadline}")
    if it.warnings:
        lines.append("⚠ " + "; ".join(it.warnings))
    return "\n".join(lines)


_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Гант плана ONE</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font:13px/1.4 -apple-system,Segoe UI,Roboto,Arial,sans-serif; color:#222; background:#fff; }}
  header {{ padding:12px 16px; border-bottom:1px solid #e3e3e3; position:sticky; top:0; background:#fff; z-index:5; }}
  h1 {{ margin:0 0 4px; font-size:16px; }}
  .meta {{ color:#666; font-size:12px; }}
  .legend {{ margin-top:6px; display:flex; gap:14px; flex-wrap:wrap; font-size:12px; align-items:center; }}
  .legend i {{ display:inline-block; width:14px; height:12px; border-radius:2px; margin-right:5px; vertical-align:-1px; }}
  .gantt {{ overflow-x:auto; position:relative; }}
  .axis {{ display:flex; position:sticky; top:0; z-index:3; }}
  .axis .spacer {{ flex:0 0 {label_w}px; position:sticky; left:0; background:#fafafa; z-index:4; border-right:1px solid #e3e3e3; }}
  .axis .axis-track {{ position:relative; height:24px; flex:0 0 {total_px}px; background:#fafafa; border-bottom:1px solid #e3e3e3; }}
  .mtick {{ position:absolute; top:0; height:24px; border-left:1px solid #d8d8d8; padding-left:4px; font-size:11px; color:#888; line-height:24px; }}
  .team {{ display:flex; height:28px; background:#eef3f8; border-top:2px solid #cfdae8; border-bottom:1px solid #cfdae8; }}
  .team-name {{ flex:0 0 {label_w}px; position:sticky; left:0; background:#eef3f8; z-index:2; font-weight:700;
                line-height:28px; padding:0 8px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .team-count {{ color:#7a93ad; font-weight:400; }}
  .team-track {{ flex:0 0 {total_px}px; }}
  .row {{ display:flex; height:{row_h}px; border-bottom:1px solid #f2f2f2; }}
  .row:hover {{ background:#f7fbff; }}
  .label {{ flex:0 0 {label_w}px; position:sticky; left:0; background:#fff; z-index:2; border-right:1px solid #e3e3e3;
            padding:0 8px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; line-height:{row_h}px; }}
  .row:hover .label {{ background:#f7fbff; }}
  .label.breach {{ box-shadow: inset 3px 0 0 #E45756; }}
  .imp {{ display:inline-block; min-width:34px; text-align:right; color:#999; font-variant-numeric:tabular-nums; margin-right:6px; }}
  .label a {{ color:#1a73e8; text-decoration:none; font-weight:600; }}
  .nm {{ color:#444; }}
  .track {{ position:relative; flex:0 0 {total_px}px;
            background:repeating-linear-gradient(90deg,#fff 0,#fff {wd_px}px,#f4f4f4 {wd_px}px,#f4f4f4 {wk_px}px); }}
  .seg {{ position:absolute; top:5px; height:{seg_h}px; border-radius:3px; opacity:.92; }}
  .seg.СА {{ background:#4C78A8; }}
  .seg.Dev {{ background:#59A14F; }}
  .seg.Тест {{ background:#E8A838; }}
  .seg.Ожидание {{ background:#B6ADA5; opacity:.6; background-image:repeating-linear-gradient(45deg,transparent,transparent 3px,rgba(255,255,255,.5) 3px,rgba(255,255,255,.5) 6px); }}
  .mark {{ position:absolute; top:2px; width:0; height:{row_h2}px; }}
  .mark.rel {{ border-left:2px solid #7B4FA8; }}
  .mark.rel::after {{ content:"◆"; position:absolute; left:-5px; top:-2px; color:#7B4FA8; font-size:9px; }}
  .mark.dl {{ border-left:2px dashed #999; }}
  .mark.dl.breach {{ border-left:2px solid #E45756; }}
  .today {{ position:absolute; top:0; width:0; border-left:2px solid #E45756; z-index:1; opacity:.55; }}
  .today-lab {{ position:absolute; top:0; transform:translateX(-50%); font-size:10px; color:#E45756; background:#fff; padding:0 3px; }}
  #tip {{ position:fixed; z-index:20; max-width:420px; background:#222; color:#fff; padding:8px 10px; border-radius:6px;
          font-size:12px; white-space:pre-line; pointer-events:none; display:none; box-shadow:0 4px 14px rgba(0,0,0,.3); }}
</style></head><body>
<header>
  <h1>Гант плана ONE · задач: {planned_n}</h1>
  <div class="meta">Период {date_from} — {date_to}. Источник ёмкости: {capacity}. Расчёт от {generated}.</div>
  <div class="legend">
    <span><i style="background:#4C78A8"></i>СА</span>
    <span><i style="background:#59A14F"></i>Разработка</span>
    <span><i style="background:#E8A838"></i>Тест-буфер</span>
    <span><i style="background:#B6ADA5"></i>Ожидание релиза</span>
    <span><i style="background:#7B4FA8"></i>◆ Релиз/ПДЗ</span>
    <span><i style="border:1px dashed #999;background:#fff"></i>Дедлайн (красный — срыв)</span>
  </div>
</header>
<div class="gantt" id="gantt">
  <div class="axis"><div class="spacer"></div><div class="axis-track">{axis}</div></div>
  {rows}
</div>
<div id="tip"></div>
<script>
  var g = document.getElementById('gantt');
  // линия «сегодня» на всю высоту контента
  var line = document.createElement('div'); line.className='today';
  line.style.left = ({label_w} + {today_x}) + 'px'; line.style.height = g.scrollHeight + 'px';
  var lab = document.createElement('div'); lab.className='today-lab'; lab.textContent='сегодня';
  lab.style.left = ({label_w} + {today_x}) + 'px';
  g.appendChild(line); g.appendChild(lab);
  // тултипы
  var tip = document.getElementById('tip');
  document.querySelectorAll('.row').forEach(function(r){{
    r.addEventListener('mousemove', function(e){{
      tip.textContent = r.getAttribute('data-tip'); tip.style.display='block';
      var x=e.clientX+14, y=e.clientY+14;
      if(x+430>window.innerWidth) x=e.clientX-430;
      tip.style.left=x+'px'; tip.style.top=y+'px';
    }});
    r.addEventListener('mouseleave', function(){{ tip.style.display='none'; }});
  }});
</script>
</body></html>"""
