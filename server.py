#!/usr/bin/env python3
"""Focus backend: hosts file management + time statistics."""
import json
import os
import sys
import time
import threading
import subprocess
import argparse
import webbrowser
import re
from datetime import datetime, timedelta
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

HOSTS_PATH = r"C:\Windows\System32\drivers\etc\hosts"
BLOCK_MARKER_START = "# === FOCUS BLOCK START ==="
BLOCK_MARKER_END = "# === FOCUS BLOCK END ==="
REDIRECT_IP = "127.0.0.1"
# 只允许合法域名（字母/数字/连字符/点，至少一个点，不以点或连字符开头/结尾）
SITE_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")

def clean_sites(sites):
    """校验屏蔽站点列表：只接受合法域名，非法输入整体拒绝（返回 None）。"""
    if not isinstance(sites, list):
        return None
    out = []
    for s in sites:
        if not isinstance(s, str):
            return None
        s = s.strip().lower()
        if not SITE_RE.fullmatch(s):
            return None
        if s not in out:
            out.append(s)
    return out

def valid_num(v, lo, hi, default=0):
    """数值校验：非数字或超范围返回 default，防止脏数据污染统计。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    if n < lo or n > hi:
        return default
    return n

def clean_str(v, max_len=100):
    """字符串清洗：非字符串返回空串，超长截断。"""
    if not isinstance(v, str):
        return ""
    return v.strip()[:max_len]

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
    """原子写入：先写 .tmp 再 os.replace，带重试退避"""
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Windows 偶发 PermissionError，重试 3 次
        for _ in range(3):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.1)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass

# --- Stats cache (avoid disk I/O on repeated reads) ---
_stats_cache = {"data": None, "mtime": 0}

def get_stats_cached():
    """Return cached stats if file mtime unchanged, else reload."""
    try:
        mtime = STATS_FILE.stat().st_mtime
    except FileNotFoundError:
        return get_default_stats()
    if mtime == _stats_cache["mtime"] and _stats_cache["data"] is not None:
        return _stats_cache["data"]
    data = load_json(STATS_FILE, get_default_stats())
    _stats_cache["data"] = data
    _stats_cache["mtime"] = mtime
    return data

def invalidate_stats_cache():
    _stats_cache["mtime"] = 0

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
                
        # 异步刷新 DNS，不阻塞 API 响应
        threading.Thread(target=lambda: subprocess.run(
            ["ipconfig", "/flushdns"], capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        ), daemon=True).start()
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
        "weight_logs": {},
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

def merge_archive_segments(stats):
    """把归档文件中缺失日期的 segments/timeline 合并回 stats.daily_logs，
    供 /api/stats 返回，保证前端科目统计能看到完整历史数据。"""
    try:
        if not ARCHIVE_FILE.exists():
            return stats
        arc = load_json(ARCHIVE_FILE, {"daily_logs": {}})
        arc_logs = arc.get("daily_logs") or {}
        dls = stats.setdefault("daily_logs", {})
        merged_any = False
        for d_date, d_content in arc_logs.items():
            segs = d_content.get("segments") or []
            tl = d_content.get("timeline") or []
            if not segs and not tl:
                continue
            if d_date not in dls:
                # 归档日期在 stats.json 缺失：补全整条
                dls[d_date] = {
                    "date": d_date,
                    "segments": segs,
                    "timeline": tl,
                    "total_time": d_content.get("total_time", 0),
                    "pomodoros": d_content.get("pomodoros", 0),
                }
                merged_any = True
            else:
                # 存在但 segments 被抽走（stats.json 只有 total_time）
                dl = dls[d_date]
                if not dl.get("segments") and segs:
                    dl["segments"] = segs
                    merged_any = True
                if not dl.get("timeline") and tl:
                    dl["timeline"] = tl
                    merged_any = True
        if merged_any:
            _stats_cache["mtime"] = 0  # 不落盘，仅返回时合并
    except Exception:
        pass
    return stats

def rollover_if_new_day(stats):
    today = effective_date_str()
    if stats.get("today") != today:
        stats["today"] = today
        stats["today_time"] = 0
        stats["today_pomodoros"] = 0
        archive_old_data(stats, keep_days=30)
        save_json(STATS_FILE, stats)
        invalidate_stats_cache()

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
                stats = get_stats_cached()
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

        elif path == "/api/config":
            with config_lock:
                config = load_json(CONFIG_FILE, {"sites": [], "active": False})
                config.setdefault("keep_block_on_break", False)
            self._send_json(config)

        elif path == "/api/stats":
            with stats_lock:
                stats = get_stats_cached()
                rollover_if_new_day(stats)
                merge_archive_segments(stats)
            self._send_json(stats)

        elif path == "/api/stats/daily":
            qs = parse_qs(urlparse(self.path).query)
            date = qs.get("date", [effective_date_str()])[0]
            with stats_lock:
                stats = get_stats_cached()
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
                stats = get_stats_cached()
            review = stats.get("reviews", {}).get(date)
            if review:
                self._send_json(review)
            else:
                self._send_json({"date": date, "text": None})

        elif path == "/api/stats/evaluate/data":
            # 为指定日期生成/返回评价数据 txt（可下载）
            qs = parse_qs(urlparse(self.path).query)
            date = qs.get("date", [effective_date_str()])[0]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                self._send_json({"error": "invalid date, expected YYYY-MM-DD"}, 400)
                return
            force_plan = qs.get("plan", [None])[0]
            # 先检查是否已有生成的 txt 文件
            txt_path = DATA_DIR / f"evaluate_{date}.txt"
            # 只有当没有收到强制切换要求时，才读取缓存。有强制要求就一律重新生成
            if txt_path.exists() and not force_plan:
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
                stats = get_stats_cached()
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

            _subj_names = {"math": "数学", "cs": "408", "eng": "英语", "pol": "政治", "sport": "运动"}
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
            # ====== 手动强制切换 AB 安排 ======
            if force_plan in ["A", "B"]:
                mode = force_plan
                if mode == "A":
                    schedule = "无课，全天自主"
                    has_class = False
                elif mode == "B":
                    schedule = "有特定安排或轻量日"
                    has_class = True
            # ===================================
            target_h = "3-6h（约5.5h为满档）" if not has_class else "≥3h"
            target_p = "10-11（含运动1个）" if not has_class else "≥4"

            # 构建 txt
            lines = []
            lines.append("═══════════════════════════════════════════")
            lines.append(f"  考研学习数据提取 — {date}（{weekday}）")
            lines.append("═══════════════════════════════════════════")
            lines.append("")
            lines.append("【基本信息】")
            lines.append(f"  日期：{date} {weekday}")
            lines.append(f"  日程分类：{mode}安排（{schedule}）")
            lines.append(f"  目标：{target_h}小时 / {target_p}番茄")
            lines.append("")
            lines.append("【核心统计】")
            lines.append(f"  - total_pomodoros: {date_pomodoros} 个")
            lines.append(f"  - segment_count: {len(segments)} 段")
            lines.append(f"  - total_time: {date_total_seconds} 秒（{date_minutes} 分钟 / {date_hours} 小时）")
            lines.append(f"  - 权威完成番茄钟数：{date_pomodoros} 个")
            lines.append(f"  - 权威总学习时长：{date_minutes} 分钟（{date_total_seconds} 秒）")
            lines.append("")
            lines.append("【科目分布】")
            for subj in ["math", "cs", "eng", "pol", "sport"]:
                if subj in subject_breakdown:
                    info = subject_breakdown[subj]
                    name = _subj_names.get(subj, subj)
                    mins = round(info["seconds"] / 60)
                    lines.append(f"  {name}：{info['segments']}段，{mins}分钟")
            other_subjs = [k for k in subject_breakdown if k not in ["math", "cs", "eng", "pol", "sport"]]
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

        elif path == "/api/stats/weights":
            with stats_lock:
                stats = get_stats_cached()
            self._send_json(stats.get("weight_logs", {}))

        elif path == "/api/stats/weight":
            qs = parse_qs(urlparse(self.path).query)
            date = qs.get("date", [effective_date_str()])[0]
            with stats_lock:
                stats = get_stats_cached()
                wl = stats.get("weight_logs", {})
                weight = wl.get(date)
            self._send_json({"date": date, "weight": weight})

        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/block/start":
            with hosts_lock, config_lock:
                config = load_json(CONFIG_FILE, {"sites": ["bilibili.com", "weibo.com", "zhihu.com", "tieba.baidu.com"]})
                sites = body.get("sites") or config.get("sites", [])
                sites = clean_sites(sites)
                if sites is None:
                    self._send_json({"ok": False, "error": "invalid_site", "message": "屏蔽站点格式不合法，仅支持域名（如 bilibili.com）。"}, 400)
                    return
                ok, msg = apply_blocking(sites)
                if ok:
                    config["sites"] = sites
                    config["active"] = True
                    config.pop("unblock", None)
                    save_json(CONFIG_FILE, config)
            self._send_json({"ok": ok, "message": msg}, 200 if ok else 403)

        elif path == "/api/block/stop":
            # 手动/自动解除均立即生效（已移除 15 分钟冷却与理由要求）
            with hosts_lock, config_lock:
                ok, msg = clear_blocking()
                if ok:
                    config = load_json(CONFIG_FILE, {"sites": [], "active": False})
                    config["active"] = False
                    config.pop("unblock", None)
                    save_json(CONFIG_FILE, config)
            self._send_json({"ok": ok, "message": msg}, 200 if ok else 403)

        elif path == "/api/block/sites":
            with config_lock:
                sites = body.get("sites", [])
                sites = clean_sites(sites)
                if sites is None:
                    self._send_json({"ok": False, "error": "invalid_site", "message": "屏蔽站点格式不合法，仅支持域名（如 bilibili.com）。"}, 400)
                    return
                config = load_json(CONFIG_FILE, {"sites": [], "active": False})
                config["sites"] = sites
                save_json(CONFIG_FILE, config)
                if config.get("active"):
                    with hosts_lock:
                        apply_blocking(sites)
            self._send_json({"ok": True, "sites": sites})

        elif path == "/api/config":
            with config_lock:
                config = load_json(CONFIG_FILE, {"sites": [], "active": False})
                config.setdefault("keep_block_on_break", False)
                if isinstance(body, dict) and "keep_block_on_break" in body:
                    config["keep_block_on_break"] = bool(body["keep_block_on_break"])
                save_json(CONFIG_FILE, config)
            self._send_json({"ok": True, "config": config})

        elif path == "/api/stats/pause":
            with stats_lock:
                stats = get_stats_cached()
                stats.setdefault("pauses", []).append({
                    "date": effective_date_str() + " " + datetime.now().strftime("%H:%M"),
                    "phase": (body.get("phase") or "") if isinstance(body, dict) else "",
                    "reason": (body.get("reason") or "") if isinstance(body, dict) else "",
                })
                save_json(STATS_FILE, stats)
                invalidate_stats_cache()
            self._send_json({"ok": True})

        elif path == "/api/stats/session":
            with stats_lock:
                stats = get_stats_cached()
                rollover_if_new_day(stats)
                duration = valid_num(body.get("duration", 0), 0, 86400)
                pomodoros = valid_num(body.get("pomodoros", 0), 0, 100)
                timeline = body.get("timeline", [])
                if not isinstance(timeline, list):
                    timeline = []
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
                invalidate_stats_cache()
            self._send_json({"ok": True, "stats": stats})

        elif path == "/api/stats/segment":
            with stats_lock:
                stats = get_stats_cached()
                rollover_if_new_day(stats)
                today = effective_date_str()
                subject = clean_str(body.get("subject", ""), 50)
                start_time = clean_str(body.get("start_time", datetime.now().strftime("%H:%M")), 10)
                end_time = clean_str(body.get("end_time", datetime.now().strftime("%H:%M")), 10)
                duration = valid_num(body.get("duration", 0), 0, 86400)
                pomodoros = valid_num(body.get("pomodoros", 0), 0, 100)

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
                invalidate_stats_cache()
            self._send_json({"ok": True, "daily": daily})

        elif path == "/api/stats/review":
            with stats_lock:
                stats = get_stats_cached()
                date = body.get("date", effective_date_str())
                is_delete = body.get("delete", False)

                if is_delete:
                    # 删除该日期的评价
                    if "reviews" in stats and date in stats["reviews"]:
                        del stats["reviews"][date]
                        save_json(STATS_FILE, stats)
                        invalidate_stats_cache()
                    self._send_json({"ok": True, "date": date, "deleted": True})
                else:
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
                    invalidate_stats_cache()
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
                stats = get_stats_cached()
                date = body.get("date")
                if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                    self._send_json({"error": "invalid date"}, 400)
                    return
                start_time = clean_str(body.get("start_time", ""), 10)
                end_time = clean_str(body.get("end_time", ""), 10)
                duration = valid_num(body.get("duration", 0), 0, 86400)
                pomodoros = valid_num(body.get("pomodoros", 0), 0, 100)
                subject = clean_str(body.get("subject", ""), 50)

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
                invalidate_stats_cache()
            self._send_json({"ok": True})

        elif path == "/api/stats/segment/delete":
            with stats_lock:
                stats = get_stats_cached()
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
                invalidate_stats_cache()
                self._send_json({"ok": True})

        elif path == "/api/config/browser":
            with config_lock:
                config = load_json(CONFIG_FILE, {"sites": [], "active": False})
                browser = body.get("browser", "system")
                config["browser"] = browser
                save_json(CONFIG_FILE, config)
            self._send_json({"ok": True, "browser": browser})

        elif path == "/api/stats/routine":
            with stats_lock:
                stats = get_stats_cached()
                rollover_if_new_day(stats)
                date = body.get("date", effective_date_str())
                r_type = body.get("type")  # "wake" or "sleep"
                r_time = body.get("time")

                if "daily_logs" not in stats:
                    stats["daily_logs"] = {}
                if date not in stats["daily_logs"]:
                    stats["daily_logs"][date] = {"date": date, "segments": [], "total_time": 0, "pomodoros": 0}

                # 记录起床或睡觉时间
                if r_type in ["wake", "sleep"]:
                    if r_time is None or r_time == "" or (isinstance(r_time, str) and re.fullmatch(r"\d{2}:\d{2}", r_time)):
                        stats["daily_logs"][date][r_type + "_time"] = r_time if r_time is not None else ""
                    else:
                        self._send_json({"ok": False, "error": "invalid time, expected HH:MM"}, 400)
                        return

                save_json(STATS_FILE, stats)
                invalidate_stats_cache()
            self._send_json({"ok": True})

        elif path == "/api/stats/weight":
            with stats_lock:
                stats = get_stats_cached()
                date = body.get("date", effective_date_str())
                try:
                    weight = float(body.get("weight"))
                except (TypeError, ValueError):
                    weight = None
                if weight is None or not (20 <= weight <= 300):
                    self._send_json({"error": "invalid weight"}, 400)
                    return
                wl = stats.setdefault("weight_logs", {})
                wl[date] = weight
                save_json(STATS_FILE, stats)
                invalidate_stats_cache()
            self._send_json({"ok": True, "date": date, "weight": weight})

        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/block/sites/"):
            domain = unquote(path.split("/api/block/sites/")[1])
            if not SITE_RE.fullmatch(domain.strip().lower()):
                self._send_json({"error": "invalid site"}, 400)
                return
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


# --- Browser launcher (migrated from launch_browser.py) ---
BROWSER_PATHS = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "firefox": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ],
    "brave": [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
}

def find_browser(browser_name):
    paths = BROWSER_PATHS.get(browser_name, [])
    for p in paths:
        if os.path.isfile(p):
            return p
    return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-browser", action="store_true",
                        help="Open UI in browser after server starts")
    args = parser.parse_args()

    print(f"Focus backend starting on http://localhost:{PORT}", flush=True)
    print(f"Stats file: {STATS_FILE}", flush=True)
    print(f"Config file: {CONFIG_FILE}", flush=True)

    hosts = read_hosts()
    if hosts is None:
        print("WARNING: Cannot access hosts file. Run on Windows as Administrator for blocking.", flush=True)
    else:
        print("Hosts file: OK (blocking available)", flush=True)

    # 后端就绪后自动打开浏览器（保留 --app 沉浸模式）
    if args.launch_browser:
        def open_ui():
            time.sleep(0.5)  # 等 HTTP 服务就绪
            with config_lock:
                config = load_json(CONFIG_FILE, {})
            browser_name = config.get("browser", "system")
            html_url = "file:///" + (DATA_DIR / "index.html").as_posix()

            if browser_name != "system":
                exe = find_browser(browser_name)
                if exe:
                    # Chrome/Edge/Brave 用 --app 无边框模式
                    if browser_name in ("chrome", "edge", "brave"):
                        subprocess.Popen([exe, "--app=" + html_url],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        subprocess.Popen([exe, html_url],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
            # fallback: 系统默认浏览器
            webbrowser.open(html_url)

        threading.Thread(target=open_ui, daemon=True).start()

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
