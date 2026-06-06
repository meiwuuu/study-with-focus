#!/usr/bin/env python3
"""Focus backend: hosts file management + time statistics."""
import json
import os
import sys
import time
import threading
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

HOSTS_PATH = r"C:\Windows\System32\drivers\etc\hosts"
BLOCK_MARKER_START = "# === FOCUS BLOCK START ==="
BLOCK_MARKER_END = "# === FOCUS BLOCK END ==="
REDIRECT_IP = "127.0.0.1"

def effective_date_str():
    """凌晨4点前算前一天"""
    now = datetime.now()
    if now.hour < 4:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")

DATA_DIR = Path(__file__).parent
STATS_FILE = DATA_DIR / "stats.json"
CONFIG_FILE = DATA_DIR / "config.json"
ARCHIVE_FILE = DATA_DIR / "stats_archive.json"
PORT = 8765

# --- Thread safety ---
stats_lock = threading.Lock()
config_lock = threading.Lock()
hosts_lock = threading.Lock()
archive_lock = threading.Lock()

# --- helpers ---

def load_json(path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return default

def save_json(path, data):
    """原子写入：先写 .tmp 再 os.replace"""
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass

def read_hosts():
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8", newline=None) as f:
            return f.read()
    except (PermissionError, FileNotFoundError):
        return None

def read_hosts_lines():
    """读取 hosts 为行列表，并在代码内部统一将所有换行符转换为 \\n。"""
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8", newline=None) as f:
            return f.readlines()
    except (PermissionError, FileNotFoundError):
        return None

def write_hosts_lines(lines):
    """将行列表写回文件，自动保留最新5份带时间戳的备份。"""
    try:
        # 写入前先进行轮转备份
        try:
            import shutil
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            hosts_dir = Path(HOSTS_PATH).parent
            bak_path = hosts_dir / f"hosts.{timestamp}.bak"
            
            if Path(HOSTS_PATH).exists():
                shutil.copy2(HOSTS_PATH, bak_path)

            # 清理过老的备份（保留最近5个）
            bak_files = sorted(hosts_dir.glob("hosts.*.bak"), key=os.path.getmtime)
            while len(bak_files) > 5:
                try:
                    bak_files.pop(0).unlink()
                except Exception:
                    pass
        except Exception:
            pass # 备份失败不阻塞核心写入逻辑

        with open(HOSTS_PATH, "w", encoding="utf-8", newline=None) as f:
            f.writelines(lines)
            # 确保文件最终以换行符结尾
            if lines and not lines[-1].endswith("\n"):
                f.write("\n")
                
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        return True
    except PermissionError:
        return False

def get_blocked_sites():
    lines = read_hosts_lines()
    if lines is None:
        return []
    sites = []
    in_block = False
    for line in lines:
        if BLOCK_MARKER_START in line:
            in_block = True
            continue
        if BLOCK_MARKER_END in line:
            in_block = False
            continue
        if in_block and line.strip() and not line.strip().startswith("#"):
            parts = line.strip().split()
            if len(parts) >= 2:
                sites.append(parts[1])
    return sites

def is_blocking_active():
    hosts = read_hosts()
    if hosts is None:
        return None
    return BLOCK_MARKER_START in hosts

def apply_blocking(sites):
    lines = read_hosts_lines()
    if lines is None:
        return False, "Permission denied: run as Administrator"

    new_lines = []
    skip = False
    for line in lines:
        if BLOCK_MARKER_START in line:
            skip = True
            continue
        if BLOCK_MARKER_END in line:
            skip = False
            continue
        if not skip:
            new_lines.append(line)

    # 智能插入：确保新区块前有适当的换行，但不破坏原有的尾部空行排版
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    if new_lines and new_lines[-1].strip() != "":
        new_lines.append("\n")

    new_lines.append(BLOCK_MARKER_START + "\n")
    for site in sites:
        new_lines.append(f"{REDIRECT_IP} {site}\n")
    new_lines.append(BLOCK_MARKER_END + "\n")

    result = write_hosts_lines(new_lines)
    if not result:
        return False, "Permission denied: run as Administrator"
    return True, "ok"

def clear_blocking():
    lines = read_hosts_lines()
    if lines is None:
        return False, "Permission denied: run as Administrator"

    new_lines = []
    skip = False
    for line in lines:
        if BLOCK_MARKER_START in line:
            skip = True
            continue
        if BLOCK_MARKER_END in line:
            skip = False
            continue
        if not skip:
            new_lines.append(line)

    result = write_hosts_lines(new_lines)
    if not result:
        return False, "Permission denied: run as Administrator"
    return True, "ok"

# --- stats ---

def get_default_stats():
    return {
        "today": effective_date_str(),
        "today_time": 0,      
        "today_pomodoros": 0,
        "total_time": 0,
        "total_pomodoros": 0,
        "sessions": [],
        "daily_logs": {},
    }

def archive_old_data(stats, keep_days=30):
    now = datetime.now()
    cutoff_date = now - timedelta(days=keep_days)
    cutoff_str = cutoff_date.strftime("%Y-%m-%d")

    archive_needed = False
    archived_sessions = []
    archived_daily = {}

    if "sessions" in stats:
        hot_sessions = []
        for s in stats["sessions"]:
            if s.get("date", "")[:10] < cutoff_str:
                archived_sessions.append(s)
                archive_needed = True
            else:
                hot_sessions.append(s)
        stats["sessions"] = hot_sessions

    if "daily_logs" in stats:
        for d_date, d_data in list(stats["daily_logs"].items()):
            if d_date < cutoff_str:
                if "segments" in d_data or "timeline" in d_data:
                    archived_daily[d_date] = {
                        "segments": d_data.pop("segments", []),
                        "timeline": d_data.pop("timeline", [])
                    }
                    archive_needed = True

    if archive_needed:
        with archive_lock:
            arc = load_json(ARCHIVE_FILE, {"sessions": [], "daily_logs": {}})
            arc["sessions"].extend(archived_sessions)
            for d_date, d_content in archived_daily.items():
                if d_date not in arc["daily_logs"]:
                    arc["daily_logs"][d_date] = d_content
                else:
                    arc["daily_logs"][d_date].setdefault("segments", []).extend(d_content["segments"])
                    arc["daily_logs"][d_date].setdefault("timeline", []).extend(d_content["timeline"])
            save_json(ARCHIVE_FILE, arc)

def rollover_if_new_day(stats):
    today = effective_date_str()
    if stats.get("today") != today:
        stats["today"] = today
        stats["today_time"] = 0
        stats["today_pomodoros"] = 0
        archive_old_data(stats, keep_days=30)

# --- HTTP server ---

class FocusHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  

    def _get_allowed_origin(self):
        """严格限制 CORS 来源，防范恶意网页篡改"""
        origin = self.headers.get("Origin")
        allowed_origins = [
            "null", # 直接双击本地 HTML 文件时 Origin 为 null
            f"http://localhost:{PORT}",
            f"http://127.0.0.1:{PORT}"
        ]
        if origin in allowed_origins or (origin and origin.startswith("http://localhost:")):
            return origin
        return "null"

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", self._get_allowed_origin())
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length:
                raw = self.rfile.read(length)
                return json.loads(raw.decode("utf-8"))
        except Exception:
            pass
        return {}

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", self._get_allowed_origin())
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/status":
            with stats_lock:
                stats = load_json(STATS_FILE, get_default_stats())
                rollover_if_new_day(stats)
            sites = get_blocked_sites()
            active = is_blocking_active()
            self._send_json({
                "blocking_active": active,
                "blocked_sites": sites,
                "stats": stats,
                "admin_error": active is None,
            })

        elif path == "/api/block/sites":
            sites = get_blocked_sites()
            self._send_json({"sites": sites})

        elif path == "/api/stats":
            with stats_lock:
                stats = load_json(STATS_FILE, get_default_stats())
                rollover_if_new_day(stats)
            self._send_json(stats)

        elif path == "/api/stats/daily":
            qs = parse_qs(urlparse(self.path).query)
            date = qs.get("date", [effective_date_str()])[0]
            with stats_lock:
                stats = load_json(STATS_FILE, get_default_stats())
                daily = stats.get("daily_logs", {}).get(date, {
                    "date": date,
                    "segments": [],
                    "total_time": 0,
                    "pomodoros": 0,
                })
            daily["date"] = date
            if daily.get("pomodoros", 0) == 0:
                segs = daily.get("segments", [])
                date_pomos = sum(1 for s in segs if s.get("duration", 0) >= 1500)
                if date_pomos > 0:
                    daily["pomodoros"] = date_pomos
            self._send_json(daily)

        elif path == "/api/stats/review":
            qs = parse_qs(urlparse(self.path).query)
            date = qs.get("date", [""])[0]
            with stats_lock:
                stats = load_json(STATS_FILE, get_default_stats())
            review = stats.get("reviews", {}).get(date)
            if review:
                self._send_json(review)
            else:
                self._send_json({"date": date, "text": None})

        elif path == "/api/stats/evaluate/data":
            # 为指定日期生成/返回评价数据 txt（可下载）
            qs = parse_qs(urlparse(self.path).query)
            date = qs.get("date", [effective_date_str()])[0]
            # 先检查是否已有生成的 txt 文件
            txt_path = DATA_DIR / f"evaluate_{date}.txt"
            if txt_path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition",
                    f'attachment; filename="evaluate_{date}.txt"')
                self.send_header("Access-Control-Allow-Origin", self._get_allowed_origin())
                self.send_header("Access-Control-Expose-Headers", "Content-Disposition")
                self.end_headers()
                with open(txt_path, 'rb') as f:
                    self.wfile.write(f.read())
                return
            # 没文件则实时生成
            with stats_lock:
                stats = load_json(STATS_FILE, get_default_stats())
            # 内联计算 per-date stats（避免动态导入 evaluate_watchdog.py 的跨进程问题）
            daily_logs = stats.get("daily_logs", {})
            sessions = stats.get("sessions", [])
            date_log = daily_logs.get(date, {})
            segments = date_log.get("segments", [])

            date_pomodoros = date_log.get("pomodoros", 0)
            date_total_seconds = date_log.get("total_time", 0)
            if date_pomodoros == 0 and date_total_seconds == 0 and segments:
                date_pomodoros = sum(1 for s in segments if s.get("duration", 0) >= 1500)
                date_total_seconds = sum(s.get("duration", 0) for s in segments)
            if not segments and not date_log:
                date_ses = [s for s in sessions if s.get("date", "").startswith(date)]
                date_pomodoros = sum(s.get("pomodoros", 0) for s in date_ses)
                date_total_seconds = sum(s.get("duration", 0) for s in date_ses)
            date_minutes = round(date_total_seconds / 60)
            date_hours = round(date_minutes / 60, 1)

            _subj_names = {"math": "数学", "cs": "408", "eng": "英语", "pol": "政治"}
            subject_breakdown = {}
            for seg in segments:
                subj = seg.get("subject", "other")
                if subj not in subject_breakdown:
                    subject_breakdown[subj] = {"segments": 0, "seconds": 0}
                subject_breakdown[subj]["segments"] += 1
                subject_breakdown[subj]["seconds"] += seg.get("duration", 0)

            # 星期/日程
            try:
                dt = datetime.strptime(date, "%Y-%m-%d")
                days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                weekday = days[dt.weekday()]
            except:
                weekday = "未知"
            _schedule_map = {
                "周一": "无课，全天自主", "周二": "日语选修（第3-4节 10:05-11:40 + 第7-8节 15:35-17:10）",
                "周三": "无课，全天自主", "周四": "PHP（第5-8节 14:00-17:30）",
                "周五": "无课，全天自主", "周六": "无课，全天自主", "周日": "无课，全天自主",
            }
            schedule = _schedule_map.get(weekday, "无课")
            has_class = "无课" not in schedule
            mode = "B" if has_class else "A"
            target_h = 6 if not has_class else 3
            target_p = 8 if not has_class else 4

            # 构建 txt
            lines = []
            lines.append("═══════════════════════════════════════════")
            lines.append(f"  考研学习数据提取 — {date}（{weekday}）")
            lines.append("═══════════════════════════════════════════")
            lines.append("")
            lines.append("【基本信息】")
            lines.append(f"  日期：{date} {weekday}")
            lines.append(f"  日程分类：{mode}安排（{schedule}）")
            lines.append(f"  目标：≥{target_h}小时 / ≥{target_p}番茄")
            lines.append("")
            lines.append("【核心统计】")
            lines.append(f"  - total_pomodoros: {date_pomodoros} 个")
            lines.append(f"  - segment_count: {len(segments)} 段")
            lines.append(f"  - total_time: {date_total_seconds} 秒（{date_minutes} 分钟 / {date_hours} 小时）")
            lines.append(f"  - 权威完成番茄钟数：{date_pomodoros} 个")
            lines.append(f"  - 权威总学习时长：{date_minutes} 分钟（{date_total_seconds} 秒）")
            lines.append("")
            lines.append("【科目分布】")
            for subj in ["math", "cs", "eng", "pol"]:
                if subj in subject_breakdown:
                    info = subject_breakdown[subj]
                    name = _subj_names.get(subj, subj)
                    mins = round(info["seconds"] / 60)
                    lines.append(f"  {name}：{info['segments']}段，{mins}分钟")
            other_subjs = [k for k in subject_breakdown if k not in ["math", "cs", "eng", "pol"]]
            for subj in other_subjs:
                info = subject_breakdown[subj]
                name = _subj_names.get(subj, subj)
                mins = round(info["seconds"] / 60)
                lines.append(f"  {name}：{info['segments']}段，{mins}分钟")
            if not subject_breakdown:
                lines.append("  （无科目数据）")
            lines.append("")
            lines.append("【学习段明细】")
            if segments:
                for i, seg in enumerate(segments):
                    sname = _subj_names.get(seg.get("subject", ""), seg.get("subject", "") or "未选科")
                    dur_min = round(seg.get("duration", 0) / 60)
                    lines.append(f"  #{i+1} {seg['start']}-{seg['end']} | {sname} | {dur_min}分钟")
            else:
                lines.append("  （无记录）")
            lines.append("")
            lines.append("═══════════════════════════════════════════")
            lines.append(f"  生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append("═══════════════════════════════════════════")
            # 追加完整评价规范（EVALUATE_PROMPT.md）
            lines.append("")
            lines.append("")
            prompt_file = DATA_DIR / "EVALUATE_PROMPT.md"
            if prompt_file.exists():
                with open(prompt_file, 'r', encoding='utf-8') as pf:
                    lines.append(pf.read())
            txt = "\n".join(lines)

            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition",
                f'attachment; filename="evaluate_{date}.txt"')
            self.send_header("Access-Control-Allow-Origin", self._get_allowed_origin())
            self.send_header("Access-Control-Expose-Headers", "Content-Disposition")
            self.end_headers()
            self.wfile.write(txt.encode("utf-8"))
            return

        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/block/start":
            with hosts_lock, config_lock:
                config = load_json(CONFIG_FILE, {"sites": ["bilibili.com", "weibo.com", "zhihu.com", "tieba.baidu.com"]})
                sites = body.get("sites") or config.get("sites", [])
                ok, msg = apply_blocking(sites)
                if ok:
                    config["sites"] = sites
                    config["active"] = True
                    save_json(CONFIG_FILE, config)
            self._send_json({"ok": ok, "message": msg}, 200 if ok else 403)

        elif path == "/api/block/stop":
            with hosts_lock, config_lock:
                ok, msg = clear_blocking()
                if ok:
                    config = load_json(CONFIG_FILE, {"sites": [], "active": False})
                    config["active"] = False
                    save_json(CONFIG_FILE, config)
            self._send_json({"ok": ok, "message": msg}, 200 if ok else 403)

        elif path == "/api/block/sites":
            with config_lock:
                sites = body.get("sites", [])
                config = load_json(CONFIG_FILE, {"sites": [], "active": False})
                config["sites"] = sites
                save_json(CONFIG_FILE, config)
                if config.get("active"):
                    with hosts_lock:
                        apply_blocking(sites)
            self._send_json({"ok": True, "sites": sites})

        elif path == "/api/stats/session":
            with stats_lock:
                stats = load_json(STATS_FILE, get_default_stats())
                rollover_if_new_day(stats)
                duration = body.get("duration", 0)
                pomodoros = body.get("pomodoros", 0)
                timeline = body.get("timeline", [])
                stats["today_time"] += duration
                stats["today_pomodoros"] += pomodoros
                stats["total_time"] += duration
                stats["total_pomodoros"] += pomodoros
                
                today = effective_date_str()
                stats.setdefault("daily_logs", {})
                if today not in stats["daily_logs"]:
                    stats["daily_logs"][today] = {"date": today, "segments": [], "total_time": 0, "pomodoros": 0}
                if pomodoros > 0:
                    stats["daily_logs"][today]["pomodoros"] = stats["daily_logs"][today].get("pomodoros", 0) + pomodoros
                
                session_entry = {
                    "date": effective_date_str() + " " + datetime.now().strftime("%H:%M"),
                    "duration": duration,
                    "pomodoros": pomodoros,
                }
                if timeline:
                    session_entry["timeline"] = timeline
                    dl = stats["daily_logs"].setdefault(today, {"date": today, "segments": [], "total_time": 0, "pomodoros": 0})
                    dl.setdefault("timeline", []).extend(timeline)
                stats["sessions"].append(session_entry)
                save_json(STATS_FILE, stats)
            self._send_json({"ok": True, "stats": stats})

        elif path == "/api/stats/segment":
            with stats_lock:
                stats = load_json(STATS_FILE, get_default_stats())
                rollover_if_new_day(stats)
                today = effective_date_str()
                subject = body.get("subject", "")
                start_time = body.get("start_time", datetime.now().strftime("%H:%M"))
                end_time = body.get("end_time", datetime.now().strftime("%H:%M"))
                duration = body.get("duration", 0)
                pomodoros = body.get("pomodoros", 0)

                if today not in stats["daily_logs"]:
                    stats["daily_logs"][today] = {
                        "date": today,
                        "segments": [],
                        "total_time": 0,
                        "pomodoros": 0,
                    }

                daily = stats["daily_logs"][today]
                daily["segments"].append({
                    "start": start_time,
                    "end": end_time,
                    "duration": duration,
                    "subject": subject,
                })
                daily["total_time"] += duration
                daily["pomodoros"] += pomodoros
                save_json(STATS_FILE, stats)
            self._send_json({"ok": True, "daily": daily})

        elif path == "/api/stats/review":
            with stats_lock:
                stats = load_json(STATS_FILE, get_default_stats())
                date = body.get("date", effective_date_str())
                text = body.get("text", "")
                if "reviews" not in stats:
                    stats["reviews"] = {}
                old = stats["reviews"].get(date, {})
                stats["reviews"][date] = {
                    "text": text,
                    "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "score": body.get("score", old.get("score")),
                    "grade": body.get("grade", old.get("grade")),
                    "source": body.get("source", old.get("source", "")),
                }
                save_json(STATS_FILE, stats)
            self._send_json({"ok": True, "date": date})

        elif path == "/api/stats/evaluate":
            date = body.get("date", effective_date_str())
            req_file = DATA_DIR / "evaluate_request.json"
            save_json(req_file, {"date": date, "requestedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            # 立即触发 cron 任务（异步，不等待）
            try:
                subprocess.Popen(
                    ["wsl", "/home/m5/.local/bin/hermes", "cron", "run", "034d1496c8da"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception:
                pass  # 触发失败不影响主流程，cron 每分钟也会自动跑
            self._send_json({"ok": True, "date": date, "message": "已提交评价请求，AI 处理中…"})

        elif path == "/api/stats/segment/manual":
            with stats_lock:
                stats = load_json(STATS_FILE, get_default_stats())
                date = body.get("date")
                start_time = body.get("start_time")
                end_time = body.get("end_time")
                duration = body.get("duration", 0)
                pomodoros = body.get("pomodoros", 0)
                subject = body.get("subject", "")

                if "daily_logs" not in stats:
                    stats["daily_logs"] = {}
                if date not in stats["daily_logs"]:
                    stats["daily_logs"][date] = {"date": date, "segments": [], "total_time": 0, "pomodoros": 0}

                daily = stats["daily_logs"][date]
                daily.setdefault("segments", []).append({
                    "start": start_time,
                    "end": end_time,
                    "duration": duration,
                    "subject": subject
                })
                # 按开始时间重新排序
                daily["segments"].sort(key=lambda x: x.get("start", ""))

                daily["total_time"] += duration
                daily["pomodoros"] += pomodoros

                if date == stats.get("today", effective_date_str()):
                    stats["today_time"] += duration
                    stats["today_pomodoros"] += pomodoros

                stats["total_time"] += duration
                stats["total_pomodoros"] += pomodoros

                save_json(STATS_FILE, stats)
            self._send_json({"ok": True})

        elif path == "/api/stats/segment/delete":
            with stats_lock:
                stats = load_json(STATS_FILE, get_default_stats())
                date = body.get("date")
                index = body.get("index")

                if date in stats.get("daily_logs", {}) and 0 <= index < len(stats["daily_logs"][date].get("segments", [])):
                    daily = stats["daily_logs"][date]
                    seg = daily["segments"].pop(index)
                    dur = seg.get("duration", 0)
                    # 如果该段时长 >= 1500 秒(25分钟)，则扣除 1 个番茄钟
                    pomos = 1 if dur >= 1500 else 0

                    daily["total_time"] = max(0, daily.get("total_time", 0) - dur)
                    daily["pomodoros"] = max(0, daily.get("pomodoros", 0) - pomos)

                    if date == stats.get("today", effective_date_str()):
                        stats["today_time"] = max(0, stats.get("today_time", 0) - dur)
                        stats["today_pomodoros"] = max(0, stats.get("today_pomodoros", 0) - pomos)

                    stats["total_time"] = max(0, stats.get("total_time", 0) - dur)
                    stats["total_pomodoros"] = max(0, stats.get("total_pomodoros", 0) - pomos)

                    save_json(STATS_FILE, stats)
                self._send_json({"ok": True})

        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/block/sites/"):
            domain = unquote(path.split("/api/block/sites/")[1])
            with config_lock:
                config = load_json(CONFIG_FILE, {"sites": [], "active": False})
                if domain in config.get("sites", []):
                    config["sites"].remove(domain)
                    save_json(CONFIG_FILE, config)
                    if config.get("active"):
                        apply_blocking(config["sites"])
            self._send_json({"ok": True, "sites": config.get("sites", [])})
        else:
            self._send_json({"error": "not found"}, 404)


def main():
    print(f"Focus backend starting on http://localhost:{PORT}", flush=True)
    print(f"Stats file: {STATS_FILE}", flush=True)
    print(f"Config file: {CONFIG_FILE}", flush=True)

    hosts = read_hosts()
    if hosts is None:
        print("WARNING: Cannot access hosts file. Run on Windows as Administrator for blocking.", flush=True)
    else:
        print("Hosts file: OK (blocking available)", flush=True)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), FocusHandler)
        print("Ready.", flush=True)
        server.serve_forever()
    except OSError as e:
        if e.errno in (98, 10048):  # EADDRINUSE
            print(f"\n[错误] 端口 {PORT} 已被占用！")
            print("请检查是否已在后台运行了该脚本，或者有代理软件占用了该端口。")
            print("如需更改端口，请同步修改 server.py 和 index.html 中的 API 地址。")
        else:
            print(f"\n[错误] 启动服务器失败: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nShutting down.")
        if 'server' in locals():
            server.server_close()


if __name__ == "__main__":
    main()
