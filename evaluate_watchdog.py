#!/usr/bin/env python3
"""
Watchdog: 检测 evaluate_request.json，有请求时输出上下文给 cron agent。
cron 每分钟跑一次（no_agent=False），脚本 stdout 注入 agent 上下文。
无请求时输出空（不触发 agent 动作）。

输出的 JSON 包含三部分：
1. evaluate_instructions: 评价规范（来自 EVALUATE_PROMPT.md）
2. user_background: 用户背景（日程、目标等）
3. learning_data: 学习数据（timeline、stats 等）
"""
import json
import sys
import os
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REQUEST_FILE = os.path.join(SCRIPT_DIR, "evaluate_request.json")
STATS_FILE = os.path.join(SCRIPT_DIR, "stats.json")
PROMPT_FILE = os.path.join(SCRIPT_DIR, "EVALUATE_PROMPT.md")

SUBJECT_NAMES = {"math": "数学", "cs": "408", "eng": "英语", "pol": "政治"}

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
        "A_全负荷": "07:30起床 → 08:00-11:00 数学 → 13:00-15:00 408 → 15:30-17:30 英语 → 19:00-21:00 政治/复习 → 22:30熄灯",
        "B_最低可行": "08:00起床 → 只做核心科目（数学+408），减少政治英语",
    },
    "target_hours": {"A": 6, "B": 3},
    "target_pomodoros": {"A": 8, "B": 4},
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

    sessions = stats.get("sessions", [])
    daily_logs = stats.get("daily_logs", {})
    date_log = daily_logs.get(req_date, {})

    # Collect all timeline events for this date
    date_timeline = date_log.get("timeline", [])
    for s in sessions:
        if s.get("date", "").startswith(req_date) and "timeline" in s:
            date_timeline.extend(s["timeline"])

    # Count stats — use top-level fields (already accurate) + segment details
    date_sessions = [s for s in sessions if s.get("date", "").startswith(req_date)]
    segments = date_log.get("segments", [])

    # Top-level values are the single source of truth for totals
    total_seconds = stats.get("today_time", 0)
    total_pomodoros = stats.get("today_pomodoros", 0)
    segment_count = len(segments)

    # Per-subject breakdown from segments (NOT sessions, to avoid double-counting)
    subject_breakdown = {}
    for seg in segments:
        subj = seg.get("subject", "other")
        if subj not in subject_breakdown:
            subject_breakdown[subj] = {"segments": 0, "seconds": 0}
        subject_breakdown[subj]["segments"] += 1
        subject_breakdown[subj]["seconds"] += seg.get("duration", 0)

    # Determine weekday and schedule info
    weekday = get_weekday_name(req_date)
    today_schedule = USER_BACKGROUND["schedule"].get(weekday, "无课")
    has_class = "无课" not in today_schedule
    suggested_mode = "B" if has_class else "A"
    target_h = USER_BACKGROUND["target_hours"][suggested_mode]
    target_p = USER_BACKGROUND["target_pomodoros"][suggested_mode]

    # Build complete context
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
            "total_seconds": total_seconds,
            "total_minutes": round(total_seconds / 60),
            "total_pomodoros": total_pomodoros,       # completed 25-min blocks (from top-level)
            "segment_count": segment_count,            # total learning blocks including short ones
            "subject_breakdown": subject_breakdown,    # per-subject: {math:{segments:N,seconds:S}, ...}
            "session_count": len(date_sessions),
            "segments": segments,
            "timeline": date_timeline,
            "timeline_summary": summarize_timeline(date_timeline),
            "has_timeline": len(date_timeline) > 0,
            "has_subjects": any(s.get("subject") for s in segments),
        },
    }

    print(json.dumps(context, ensure_ascii=False))

    # Delete request file so we don't process twice
    os.remove(REQUEST_FILE)

except Exception as e:
    print(json.dumps({"error": str(e)}), file=sys.stderr)
    sys.exit(1)
