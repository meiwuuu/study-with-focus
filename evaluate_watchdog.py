#!/usr/bin/env python3
"""
Watchdog: 检测 evaluate_request.json，有请求时输出上下文给 cron agent。
cron 每分钟跑一次（no_agent=False），脚本 stdout 注入 agent 上下文。
无请求时输出空（不触发 agent 动作）。

输出的 JSON 包含三部分：
1. evaluate_instructions: 评价规范（来自 EVALUATE_PROMPT.md）
2. user_background: 用户背景（日程、目标等）
3. learning_data: 学习数据（timeline、stats 等）

修复：支持任意日期——从 daily_logs[date] 提取数据，不再硬编码 today_*。
"""
import json
import sys
import os
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REQUEST_FILE = os.path.join(SCRIPT_DIR, "evaluate_request.json")
STATS_FILE = os.path.join(SCRIPT_DIR, "stats.json")
PROMPT_FILE = os.path.join(SCRIPT_DIR, "EVALUATE_PROMPT.md")

SUBJECT_NAMES = {"math": "数学", "cs": "408", "eng": "英语", "pol": "政治", "sport": "运动"}

# ═══════════ 用户背景（硬编码，定期更新） ═══════════
USER_BACKGROUND = {
    "name": "M5",
    "goal": "27届考研，目标杭州电子科技大学",
    "subjects": "数学一 + 英语一 + 408计算机综合 + 政治",
    "school": "福建师范大学旗山校区，网络工程1班",
    "schedule": {
        "周一": "无课，全天自主",
        "周二": "日语选修（第3-4节 10:05-11:40 + 第7-8节 15:35-17:10）",
        "周三": "无课，全天自主",
        "周四": "PHP（第5-8节 14:00-17:30）",
        "周五": "无课，全天自主",
        "周六": "无课，全天自主",
        "周日": "无课，全天自主",
    },
    "study_modes": {
        "A_全负荷（无课日）": "08:30起床 → 09:00-09:55 数学 → 10:00-10:25 英语 → 10:35-11:30 数学 → 12:00-13:30 午饭+午休≤30min → 14:00-15:30 408 → 15:40-16:35 408 → 16:40-17:05 英语 → 17:10-17:35 运动 → 19:00-20:30 数学真题/错题复盘 → 21:00后收手 → 23:45收工",
        "B_最低可行（有课日）": "08:00起床 → 只做核心科目（数学+408），目标≥3h",
    },
    "target_hours": {"A": "3-6h（约5.5h为满档）", "B": "≥3h"},
    "target_pomodoros": {"A": "10-11（含运动1个）", "B": "≥4"},
}

def get_weekday_name(date_str):
    """返回中文星期名"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return days[dt.weekday()]
    except:
        return "未知"


def summarize_timeline(timeline):
    """Generate a human-readable summary of the timeline events."""
    if not timeline:
        return "无精细操作数据"

    lines = []
    for ev in timeline:
        ts = ev.get("ts", "")[:19].replace("T", " ")
        action = ev.get("action", "")
        subject = SUBJECT_NAMES.get(ev.get("subject", ""), ev.get("subject", "") or "未选科")

        if action == "start":
            lines.append(f"{ts} ▶ 开始专注 ({subject})")
        elif action == "pause":
            acc = ev.get("accSeconds", 0)
            lines.append(f"{ts} ⏸ 暂停 (已专注{acc}s)")
        elif action == "resume":
            lines.append(f"{ts} ▶ 恢复专注 ({subject})")
        elif action == "complete":
            num = ev.get("pomodoroNum", "")
            secs = ev.get("totalSec", 0)
            lines.append(f"{ts} ✅ 完成番茄#{num} ({subject}, {secs}s)")
        elif action == "end":
            secs = ev.get("totalSec", 0)
            lines.append(f"{ts} ⏹ 手动结束 ({subject}, {secs}s)")
        elif action == "subject_change":
            fr = SUBJECT_NAMES.get(ev.get("from", ""), ev.get("from", "") or "未选")
            to = SUBJECT_NAMES.get(ev.get("to", ""), ev.get("to", "") or "未选")
            lines.append(f"{ts} 🔄 切换科目: {fr} → {to}")

    return "\n".join(lines)


def load_evaluate_instructions():
    """读取评价规范文档"""
    if os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, 'r', encoding='utf-8') as f:
            return f.read()
    return "评价规范文档未找到"


def build_context(req_date, stats):
    """
    为指定日期构建完整的评价上下文。
    从 daily_logs[date] 提取统计数据（而非全局 today_*）。
    """
    daily_logs = stats.get("daily_logs", {})
    sessions = stats.get("sessions", [])

    # ── 从 daily_logs 获取该日期的完整数据 ──
    date_log = daily_logs.get(req_date, {})

    # 该日期的 segments（科目分段）
    segments = date_log.get("segments", [])

    # 该日期的 timeline（精细操作日志）
    date_timeline = date_log.get("timeline", [])

    # 同时从 sessions 中补充 timeline（兼容旧数据）
    for s in sessions:
        if s.get("date", "").startswith(req_date) and "timeline" in s:
            date_timeline.extend(s["timeline"])

    # ── 核心统计：从 daily_log 取，fallback 到 session 汇总 ──
    date_pomodoros = date_log.get("pomodoros", 0)
    date_total_seconds = date_log.get("total_time", 0)

    # fallback: 如果 daily_log 没有统计数据（旧格式），从 segments/sessions 计算
    if date_pomodoros == 0 and date_total_seconds == 0 and segments:
        date_pomodoros = sum(1 for s in segments if s.get("duration", 0) >= 1500)
        date_total_seconds = sum(s.get("duration", 0) for s in segments)

    # 如果 daily_log 也没有 segments，从 sessions 计算
    if not segments and not date_log:
        date_ses = [s for s in sessions if s.get("date", "").startswith(req_date)]
        date_pomodoros = sum(s.get("pomodoros", 0) for s in date_ses)
        date_total_seconds = sum(s.get("duration", 0) for s in date_ses)

    # ── 该日期的 session 列表 ──
    date_sessions = [s for s in sessions if s.get("date", "").startswith(req_date)]

    # ── 科目分布（从 segments 计算）──
    subject_breakdown = {}
    for seg in segments:
        subj = seg.get("subject", "other")
        if subj not in subject_breakdown:
            subject_breakdown[subj] = {"segments": 0, "seconds": 0}
        subject_breakdown[subj]["segments"] += 1
        subject_breakdown[subj]["seconds"] += seg.get("duration", 0)

    segment_count = len(segments)

    # ── 星期 / 日程判断 ──
    weekday = get_weekday_name(req_date)
    today_schedule = USER_BACKGROUND["schedule"].get(weekday, "无课")
    has_class = "无课" not in today_schedule
    suggested_mode = "B" if has_class else "A"
    target_h = USER_BACKGROUND["target_hours"][suggested_mode]
    target_p = USER_BACKGROUND["target_pomodoros"][suggested_mode]

    # ── 构建上下文 ──
    context = {
        "evaluate_instructions": load_evaluate_instructions(),
        "user_background": {
            **USER_BACKGROUND,
            "target_mode": suggested_mode,
            "target_hours_today": target_h,
            "target_pomodoros_today": target_p,
            "today_weekday": weekday,
            "today_has_class": has_class,
            "today_schedule": today_schedule,
        },
        "learning_data": {
            "date": req_date,
            "total_seconds": date_total_seconds,
            "total_minutes": round(date_total_seconds / 60),
            "total_pomodoros": date_pomodoros,
            "segment_count": segment_count,
            "subject_breakdown": subject_breakdown,
            "session_count": len(date_sessions),
            "segments": segments,
            "timeline": date_timeline,
            "timeline_summary": summarize_timeline(date_timeline),
            "has_timeline": len(date_timeline) > 0,
            "has_subjects": any(s.get("subject") for s in segments),
        },
    }

    return context


def compute_per_date_stats(req_date, stats):
    """
    为指定日期计算统计数据（供 txt 输出用）。
    返回纯文本摘要。
    """
    daily_logs = stats.get("daily_logs", {})
    sessions = stats.get("sessions", [])
    date_log = daily_logs.get(req_date, {})
    segments = date_log.get("segments", [])

    date_pomodoros = date_log.get("pomodoros", 0)
    date_total_seconds = date_log.get("total_time", 0)

    if date_pomodoros == 0 and date_total_seconds == 0 and segments:
        date_pomodoros = sum(1 for s in segments if s.get("duration", 0) >= 1500)
        date_total_seconds = sum(s.get("duration", 0) for s in segments)

    if not segments and not date_log:
        date_ses = [s for s in sessions if s.get("date", "").startswith(req_date)]
        date_pomodoros = sum(s.get("pomodoros", 0) for s in date_ses)
        date_total_seconds = sum(s.get("duration", 0) for s in date_ses)

    date_minutes = round(date_total_seconds / 60)
    date_hours = round(date_minutes / 60, 1)
    segment_count = len(segments)

    # 科目分布
    subject_breakdown = {}
    for seg in segments:
        subj = seg.get("subject", "other")
        if subj not in subject_breakdown:
            subject_breakdown[subj] = {"segments": 0, "seconds": 0}
        subject_breakdown[subj]["segments"] += 1
        subject_breakdown[subj]["seconds"] += seg.get("duration", 0)

    # 日期信息
    weekday = get_weekday_name(req_date)
    schedule = USER_BACKGROUND["schedule"].get(weekday, "无课")
    has_class = "无课" not in schedule
    mode = "B" if has_class else "A"
    
    # 生成 segments 明细
    segment_lines = []
    for i, seg in enumerate(segments):
        subj = SUBJECT_NAMES.get(seg.get("subject", ""), seg.get("subject", "") or "未选科")
        dur_min = round(seg.get("duration", 0) / 60)
        segment_lines.append(f"  #{i+1} {seg['start']}-{seg['end']} | {subj} | {dur_min}分钟")

    lines = []
    lines.append(f"═══════════════════════════════════════════")
    lines.append(f"  考研学习数据提取 — {req_date}（{weekday}）")
    lines.append(f"═══════════════════════════════════════════")
    lines.append(f"")
    lines.append(f"【基本信息】")
    lines.append(f"  日期：{req_date} {weekday}")
    lines.append(f"  日程分类：{mode}安排（{schedule}）")
    lines.append(f"  目标：{USER_BACKGROUND['target_hours'][mode]}小时 / {USER_BACKGROUND['target_pomodoros'][mode]}番茄")
    lines.append(f"")
    lines.append(f"【核心统计】")
    lines.append(f"  - total_pomodoros: {date_pomodoros} 个")
    lines.append(f"  - segment_count: {segment_count} 段")
    lines.append(f"  - total_time: {date_total_seconds} 秒（{date_minutes} 分钟 / {date_hours} 小时）")
    lines.append(f"  - 权威完成番茄钟数：{date_pomodoros} 个")
    lines.append(f"  - 权威总学习时长：{date_minutes} 分钟（{date_total_seconds} 秒）")
    lines.append(f"")
    lines.append(f"【科目分布】")
    subj_order = ["math", "cs", "eng", "pol"]
    for subj in subj_order:
        if subj in subject_breakdown:
            info = subject_breakdown[subj]
            name = SUBJECT_NAMES.get(subj, subj)
            mins = round(info["seconds"] / 60)
            lines.append(f"  {name}：{info['segments']}段，{mins}分钟")
    other_subjs = [k for k in subject_breakdown if k not in subj_order]
    for subj in other_subjs:
        info = subject_breakdown[subj]
        name = SUBJECT_NAMES.get(subj, subj)
        mins = round(info["seconds"] / 60)
        lines.append(f"  {name}：{info['segments']}段，{mins}分钟")
    if not subject_breakdown:
        lines.append(f"  （无科目数据）")
    lines.append(f"")
    lines.append(f"【学习段明细】")
    if segment_lines:
        lines.extend(segment_lines)
    else:
        lines.append(f"  （无记录）")
    lines.append(f"")
    lines.append(f"═══════════════════════════════════════════")
    lines.append(f"  生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"═══════════════════════════════════════════")
    # 追加完整评价规范（EVALUATE_PROMPT.md）
    lines.append("")
    lines.append("")
    if os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, 'r', encoding='utf-8') as pf:
            lines.append(pf.read())

    return "\n".join(lines)


# ═══════════ Main ═══════════
try:
    if not os.path.exists(REQUEST_FILE):
        sys.exit(0)

    # Read request
    with open(REQUEST_FILE, 'r', encoding='utf-8') as f:
        req = json.load(f)
    req_date = req.get("date", datetime.now().strftime("%Y-%m-%d"))

    # Read stats
    stats = {}
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, 'r', encoding='utf-8') as f:
            stats = json.load(f)

    # ── 构建评价上下文（使用 per-date 数据）──
    context = build_context(req_date, stats)

    # ── 同时生成可下载的 txt 摘要 ──
    txt_summary = compute_per_date_stats(req_date, stats)
    txt_path = os.path.join(SCRIPT_DIR, f"evaluate_{req_date}.txt")
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(txt_summary)

    print(json.dumps(context, ensure_ascii=False))

    # Delete request file so we don't process twice
    os.remove(REQUEST_FILE)

except Exception as e:
    print(json.dumps({"error": str(e)}), file=sys.stderr)
    sys.exit(1)
