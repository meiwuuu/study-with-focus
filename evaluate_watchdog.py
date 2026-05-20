#!/usr/bin/env python3
"""
Watchdog: 检测 evaluate_request.json，有请求时输出上下文给 cron agent。
cron 每分钟跑一次（no_agent=False），脚本 stdout 注入 agent 上下文。
无请求时输出空（不触发 agent 动作）。
"""
import json
import sys
import os
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REQUEST_FILE = os.path.join(SCRIPT_DIR, "evaluate_request.json")
STATS_FILE = os.path.join(SCRIPT_DIR, "stats.json")

SUBJECT_NAMES = {"math": "数学", "cs": "408", "eng": "英语", "pol": "政治"}


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

    # Count stats
    date_sessions = [s for s in sessions if s.get("date", "").startswith(req_date)]
    segments = date_log.get("segments", [])

    total_seconds = sum(s.get("duration", 0) for s in date_sessions)
    total_seconds += date_log.get("total_time", 0)
    total_pomodoros = sum(s.get("pomodoros", 0) for s in date_sessions)
    total_pomodoros += date_log.get("pomodoros", 0)

    # Build context
    context = {
        "date": req_date,
        "total_seconds": total_seconds,
        "total_pomodoros": total_pomodoros,
        "session_count": len(date_sessions),
        "segments": segments,
        "timeline": date_timeline,
        "timeline_summary": summarize_timeline(date_timeline),
        "has_timeline": len(date_timeline) > 0,
        "has_subjects": any(s.get("subject") for s in segments),
    }

    print(json.dumps(context, ensure_ascii=False))

    # Delete request file so we don't process twice
    os.remove(REQUEST_FILE)

except Exception as e:
    print(json.dumps({"error": str(e)}), file=sys.stderr)
    sys.exit(1)
