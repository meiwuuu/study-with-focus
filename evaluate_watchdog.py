#!/usr/bin/env python3
"""
Watchdog: 检测 evaluate_request.json，有请求时输出上下文给 cron agent。
cron 每分钟跑一次（no_agent=False），脚本 stdout 注入 agent 上下文。
无请求时输出空（按 cron 设计：空输出不触发 agent 动作）。
"""
import json
import sys
import os
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REQUEST_FILE = os.path.join(SCRIPT_DIR, "evaluate_request.json")
STATS_FILE = os.path.join(SCRIPT_DIR, "stats.json")

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

    # Get data for requested date
    date_sessions = [s for s in sessions if s.get("date", "").startswith(req_date)]
    date_log = daily_logs.get(req_date, {})

    # Calculate stats
    total_seconds = 0
    total_pomodoros = 0
    segments = date_log.get("segments", [])

    for s in date_sessions:
        total_seconds += s.get("duration", 0)
        total_pomodoros += s.get("pomodoros", 0)

    # Also count from daily_log
    if not segments and date_sessions:
        # No segmented data, use sessions as-is
        pass

    # Build context for agent
    context = {
        "date": req_date,
        "total_seconds": total_seconds + date_log.get("total_time", 0),
        "total_pomodoros": total_pomodoros + date_log.get("pomodoros", 0),
        "session_count": len(date_sessions),
        "segments": segments if segments else [
            {"start": s["date"][-5:] if "date" in s else "--", "duration": s.get("duration", 0)}
            for s in date_sessions
        ],
        "has_subjects": len(segments) > 0,
    }

    # Print as JSON context for the agent
    print(json.dumps(context, ensure_ascii=False))

    # Delete request file so we don't process twice
    os.remove(REQUEST_FILE)

except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
