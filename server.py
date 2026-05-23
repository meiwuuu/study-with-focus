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
PORT = 8765

ARCHIVE_FILE = DATA_DIR / "stats_archive.json"

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
    except (PermissionError, FileNotFoundError) as e:
        return None

def read_hosts_lines():
    """读取 hosts 为行列表，并在代码内部统一将所有换行符转换为 \\n。"""
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8", newline=None) as f:
            return f.readlines()
    except (PermissionError, FileNotFoundError):
        return None

def write_hosts_lines(lines):
    """将行列表写回文件，并在 Windows 下自动将 \\n 统一转换为 \\r\\n。"""
    try:
        # 清理文件末尾可能由于历史原因堆积的连续空行
        while lines and lines[-1].strip() == "":
            lines.pop()
        
        # 写入前先备份（仅保留一份 .bak）
        bak = Path(HOSTS_PATH).with_suffix(".bak")
        if Path(HOSTS_PATH).exists():
            try:
                import shutil
                shutil.copy2(HOSTS_PATH, bak)
            except Exception:
                pass

        with open(HOSTS_PATH, "w", encoding="utf-8", newline=None) as f:
            f.writelines(lines)
            # 确保文件以一个标准换行符结尾
            if lines and not lines[-1].endswith("\n"):
                f.write("\n")
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=10)
        return True
    except PermissionError:
        return False

def get_blocked_sites():
    """从 hosts 文件中提取当前处于屏蔽状态的域名。"""
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
    """应用屏蔽：精准替换旧区块，采用纯 \\n 拼接，交给 Python 托管转换。"""
    lines = read_hosts_lines()
    if lines is None:
        return False, "Permission denied: run as Administrator"

    # 1. 移除可能已存在的旧区块
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

    # 2. 清理原内容尾部多余的空行
    while new_lines and new_lines[-1].strip() == "":
        new_lines.pop()

    # 3. 紧凑追加新区块（原本最后一行已自带 \\n，因此先追加一个空行 \\n 即可保持一个空行间距）
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
    """关闭屏蔽：干净地移除区块，并对文件尾部进行格式化修剪。"""
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

    # 核心修正：关闭时同样清理尾部残留的空行，使文件恢复最初的紧凑状态
    while new_lines and new_lines[-1].strip() == "":
        new_lines.pop()

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
        "daily_logs": {},     # { "2026-05-20": { segments: [...], total_time: N } }
    }

def archive_old_data(stats, keep_days=30):
    """
    冷热数据分离：将超过指定天数的 sessions 和 daily_logs 详细数据
    迁移到 stats_archive.json 中，保持主 JSON 轻量。
    """
    now = datetime.now()
    cutoff_date = now - timedelta(days=keep_days)
    cutoff_str = cutoff_date.strftime("%Y-%m-%d")

    archive_needed = False
    archived_sessions = []
    archived_daily = {}

    # 1. 筛选需要归档的 sessions
    if "sessions" in stats:
        hot_sessions = []
        for s in stats["sessions"]:
            if s.get("date", "")[:10] < cutoff_str:
                archived_sessions.append(s)
                archive_needed = True
            else:
                hot_sessions.append(s)
        stats["sessions"] = hot_sessions

    # 2. 筛选需要归档的 daily_logs 详细数组
    if "daily_logs" in stats:
        for d_date, d_data in list(stats["daily_logs"].items()):
            if d_date < cutoff_str:
                if "segments" in d_data or "timeline" in d_data:
                    archived_daily[d_date] = {
                        "segments": d_data.pop("segments", []),
                        "timeline": d_data.pop("timeline", [])
                    }
                    archive_needed = True

    # 3. 追加到归档文件
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

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
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
        self.send_header("Access-Control-Allow-Origin", "*")
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
            # Daily_log pomodoros is updated by session saves; fallback to segments for old data
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
                # Also update daily_log pomodoros
                today = effective_date_str()
                stats.setdefault("daily_logs", {})
                if today not in stats["daily_logs"]:
                    stats["daily_logs"][today] = {"date": today, "segments": [], "total_time": 0, "pomodoros": 0}
                if pomodoros > 0:
                    stats["daily_logs"][today]["pomodoros"] = stats["daily_logs"][today].get("pomodoros", 0) + pomodoros
                # total_time 由 segments 累加，session 不再重复计入（避免 double-count）
                session_entry = {
                    "date": effective_date_str() + " " + datetime.now().strftime("%H:%M"),
                    "duration": duration,
                    "pomodoros": pomodoros,
                }
                if timeline:
                    session_entry["timeline"] = timeline
                    # Merge into daily_log timeline
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
                # segment 只属于 daily_logs，不写入 sessions（session 是完整番茄）
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
            self._send_json({"ok": True, "date": date, "message": "已提交评价请求，稍后刷新查看"})

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

    server = ThreadingHTTPServer(("127.0.0.1", PORT), FocusHandler)
    print("Ready.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
