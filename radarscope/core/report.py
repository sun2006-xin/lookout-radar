"""会话汇总报表导出（ROADMAP D2）。

不引入 ReportLab / WeasyPrint 等重量级依赖，输出自包含 HTML（含内联 SVG 航迹分布图
与打印样式，浏览器 Ctrl+P 可直接存为 PDF）或 CSV 汇总表。
"""
from __future__ import annotations

import csv
import html
import time
from pathlib import Path

from .storage import Store


def _svg_scatter(rows: list[tuple], rmax: float = 10.0) -> str:
    """生成轻量的 SVG 航迹位置散点图（无需 matplotlib）。"""
    size = 260
    half = size / 2.0
    scale = (half - 16) / max(1.0, rmax)
    circles = "".join(
        f'<circle cx="{half:.1f}" cy="{half:.1f}" r="{(half - 16) * k / 3:.1f}" '
        f'fill="none" stroke="#CBD5E1" stroke-dasharray="3,3"/>'
        for k in (1, 2, 3)
    )
    pts = []
    for row in rows[::max(1, len(rows) // 400)]:
        _ts, tid, x, y = row[0], row[1], float(row[2]), float(row[3])
        sx = max(6.0, min(size - 6.0, half + x * scale))
        sy = max(6.0, min(size - 6.0, half - y * scale))
        col = "#2563EB" if int(tid) >= 0 else "#64748B"
        pts.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="2.2" fill="{col}" opacity="0.75"/>')
    axes = (
        f'<line x1="{half}" y1="8" x2="{half}" y2="{size - 8}" stroke="#E2E8F0"/>'
        f'<line x1="8" y1="{half}" x2="{size - 8}" y2="{half}" stroke="#E2E8F0"/>'
    )
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'xmlns="http://www.w3.org/2000/svg" style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px">'
        f'{axes}{circles}{"".join(pts)}</svg>'
    )


def export_session_report(store: Store, sid: int, out_path: str | Path) -> Path:
    """导出指定会话的汇总报表（.html 或 .csv）。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    sessions = {s.id: s for s in store.list_sessions(200)}
    sess = sessions.get(sid)
    st = store.session_stats(sid)
    alarms = store.alarm_summary(sid)
    tracks = store.tracks_of(sid)

    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sess.started)) if sess else "-"
    ended = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sess.ended)) if (sess and sess.ended) else "-"
    driver_name = (sess.caps.get("name") or sess.driver) if sess else f"#{sid}"
    rmax = float((sess.caps.get("range_max") if sess else None) or 10.0)

    if out.suffix.lower() == ".csv":
        with open(out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["会话ID", "雷达", "开始时间", "结束时间", "航迹点数", "最大航迹号", "告警总数"])
            w.writerow([sid, driver_name, started, ended, st["tracks_rows"], st["max_tid"], st["events"]])
            w.writerow([])
            w.writerow(["规则ID", "最后消息", "触发次数"])
            for rid, msg, cnt in alarms:
                w.writerow([rid, msg, cnt])
        return out

    alarm_rows = "".join(
        f"<tr><td>{html.escape(str(rid))}</td><td>{html.escape(str(msg))}</td><td>{cnt}</td></tr>"
        for rid, msg, cnt in alarms
    ) or '<tr><td colspan="3" style="color:#64748B">本次会话无告警记录</td></tr>'

    svg = _svg_scatter(tracks, rmax=rmax)
    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>瞭望 Lookout · 会话 #{sid} 汇总报表</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; color: #1E293B; margin: 28px auto; max-width: 820px; }}
  h1 {{ font-size: 20px; margin-bottom: 4px; }}
  .meta {{ color: #64748B; font-size: 13px; margin-bottom: 20px; }}
  .grid {{ display: flex; gap: 24px; align-items: flex-start; flex-wrap: wrap; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 13px; }}
  th, td {{ border: 1px solid #E2E8F0; padding: 7px 10px; text-align: left; }}
  th {{ background: #F8FAFC; }}
  .kpi {{ display: flex; gap: 14px; margin-bottom: 18px; }}
  .card {{ border: 1px solid #E2E8F0; border-radius: 8px; padding: 10px 14px; min-width: 120px; }}
  .card b {{ display: block; font-size: 18px; margin-top: 4px; }}
  @media print {{ body {{ margin: 12px; }} }}
</style>
</head>
<body>
  <h1>瞭望 Lookout · 会话 #{sid} 汇总报表</h1>
  <div class="meta">雷达：{html.escape(str(driver_name))} ｜ 起止：{started} ~ {ended}</div>
  <div class="kpi">
    <div class="card"><span>航迹记录</span><b>{st['tracks_rows']}</b></div>
    <div class="card"><span>独立航迹上限</span><b>{st['max_tid']}</b></div>
    <div class="card"><span>告警/事件</span><b>{st['events']}</b></div>
  </div>
  <div class="grid">
    <div>
      <h3>航迹空间分布（量程 {rmax:.1f} m）</h3>
      {svg}
    </div>
    <div style="flex:1;min-width:280px">
      <h3>告警统计</h3>
      <table><thead><tr><th>规则</th><th>内容</th><th>次数</th></tr></thead><tbody>{alarm_rows}</tbody></table>
    </div>
  </div>
</body>
</html>
"""
    out.write_text(doc, encoding="utf-8")
    return out
