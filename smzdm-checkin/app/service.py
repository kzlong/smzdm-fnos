import json
import os
import re
import socketserver
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from loguru import logger
from smzdm_bot.client import SmzdmClient
from smzdm_bot.config.models import NotificationConfig, TaskPolicyConfig, UserConfig
from smzdm_bot.exceptions import APIError, ConfigurationError
from smzdm_bot.notifications.channels import BarkChannel, bark_targets
from smzdm_bot.protocol import parse_cookie_header
from smzdm_bot.tasks.checkin import claim_extra_checkin_reward, perform_daily_checkin
from smzdm_bot.tasks.daily import DailyTaskExecutor
from smzdm_bot.tasks.execution import wait_for_random_delay
from smzdm_bot.tasks.lottery import enter_free_crowd_lotteries, perform_task_lottery


PREFIX = "/app/smzdm-checkin"
SHANGHAI = timezone(timedelta(hours=8))
UI = Path(__file__).resolve().parent / "ui"
TASK_DEFAULTS = {"extra_rewards": True, "lottery": False, "daily": False, "follow": True}
ASSETS = {
    "": ("index.html", "text/html; charset=utf-8"),
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def normalize_cookie(cookie):
    cookie = re.sub(r"^cookie:\s*", "", cookie, flags=re.IGNORECASE)
    pairs = []
    for entry in cookie.split(";"):
        if not entry.strip():
            continue
        name, delimiter, value = entry.partition("=")
        if not delimiter or not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name.strip()):
            raise ValueError("Cookie 格式无效，请用分号分隔各个名称和值")
        pairs.append(f"{name.strip()}={value.strip()}")
    normalized = "; ".join(pairs)
    if not parse_cookie_header(normalized).get("sess", "").strip():
        raise ValueError("请输入 App Cookie 的完整值，须包含 sess")
    return normalized


def device_platform(cookie_fields, selected):
    if selected != "auto":
        return selected
    marker = cookie_fields.get("device_smzdm", "").lower()
    if marker in {"iphone", "ios"}:
        return "iphone"
    if marker == "android":
        return "android"
    device_name = cookie_fields.get("device_name", "").lower()
    return "iphone" if device_name.startswith(("iphone", "ipad")) else "android"


class CheckinClient(SmzdmClient):
    def _build_app_headers(self):
        headers = super()._build_app_headers()
        # Device metadata is URL-decoded upstream when composing the User-Agent.
        # Escape non-ASCII and control bytes without changing existing percent escapes.
        for name in ("Cookie", "User-Agent"):
            headers[name] = re.sub(r"[^\x20-\x7e]+", lambda match: quote(match[0], safe=""), headers[name])
        return headers


def checkin(account):
    cookie = account["cookie"]
    fields = parse_cookie_header(cookie)
    platform = device_platform(fields, account["platform"])
    sk = account["sk"] if platform == "android" else ""
    if platform == "android" and not sk:
        missing = [name for name in ("smzdm_id", "device_id") if not fields.get(name)]
        if missing:
            return f"Android Cookie 缺少 {'、'.join(missing)}。如用 iPhone 抓包，请在编辑账号里选择 iPhone", False
    if fields.get("device_smzdm") != platform:
        cookie = re.sub(r"(?:^|;)\s*device_smzdm\s*=[^;]*", "", cookie).strip("; ")
        cookie = f"{cookie}; device_smzdm={platform}"
    try:
        with CheckinClient(UserConfig(cookie=cookie, security_key=sk)) as client:
            try:
                result = perform_daily_checkin(client)
                if result.consecutive_days < 1:
                    return "无法确认签到结果，请在 App 查看", False
                message = f"签到成功 · 连签 {result.consecutive_days} 天"
            except APIError as error:
                if not re.search(r"已.{0,8}签到|重复签到|签到过|签过到", str(error)):
                    raise
                message = "今日已签到"
            options = TASK_DEFAULTS | json.loads(account.get("task_options", "{}"))
            return "\n".join([message, *run_optional_tasks(client, options)]), True
    except ConfigurationError:
        return "Android 凭证无法生成 SK，请更新 App Cookie 或填写同机 SK", False
    except APIError as error:
        message = str(error)
        if re.search(r"已.{0,8}签到|重复签到|签到过|签过到", message):
            return "今日已签到", True
        if re.search(r"未登录|请.{0,4}登录|登录.{0,6}(过期|失效)|Cookie.{0,10}(过期|失效)", message, re.I):
            return "登录状态失效，请更新 Cookie", False
        if re.search(r"风控|验证码|操作频繁|访问异常", message):
            return "平台要求验证，请在手机 App 处理后重试", False
        return f"上游拒绝签到（错误码 {error.error_code or '未知'}）", False
    except httpx.HTTPStatusError as error:
        return f"签到请求失败（HTTP {error.response.status_code}）", False
    except httpx.RequestError:
        return "网络请求失败，请检查 NAS 网络", False
    except UnicodeError:
        return "签到请求包含无法编码的字符，请重新复制 App Cookie", False


def run_optional_tasks(client, options):
    operations = []
    if options["extra_rewards"]:
        operations.append(("连签额外奖励", lambda: "已领取" if claim_extra_checkin_reward(client) else "暂无可领取奖励"))
    if options["daily"]:
        policy = TaskPolicyConfig(enable_follow_tasks=options["follow"], enable_activity_reward_claims=True)
        operations.append(("每日任务", lambda: DailyTaskExecutor(client, policy).execute().to_message()))
    if options["lottery"]:
        operations.append(("APP 任务抽奖", lambda: perform_task_lottery(client).message))
        operations.append(("免费幸运屋", lambda: f"已参与 {enter_free_crowd_lotteries(client)} 个免费抽奖"))
    results = []
    for name, operation in operations:
        try:
            wait_for_random_delay((2, 5))
            results.append(f"{name} · {operation()}")
        except APIError as error:
            results.append(f"{name} · 请求失败（错误码 {error.error_code or '未知'}）")
        except Exception as error:
            results.append(f"{name} · 执行失败（{type(error).__name__}）")
    return results


def validate_bark(value):
    if len(value) > 2048 or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("Bark 地址格式无效")
    for target in bark_targets(value):
        if re.fullmatch(r"[A-Za-z0-9_-]+", target):
            continue
        try:
            url = urlsplit(target)
            if (url.scheme in {"http", "https"} and url.hostname and url.path.strip("/")
                    and not url.username and not url.password and not url.fragment
                    and not any(char.isspace() for char in target)):
                continue
        except ValueError:
            pass
        raise ValueError("请输入 Bark 设备 Key 或 HTTP(S) 推送地址，多个目标用英文逗号分隔")
    return ",".join(bark_targets(value))


def push_bark(account, source, message, success):
    mode = account["bark_mode"]
    if mode == "off" or (mode == "failure" and success):
        return ""
    result_text = "签到成功" if success else "签到失败"
    trigger = "自动" if source == "auto" else "手动"
    try:
        channel = BarkChannel(NotificationConfig(bark_push_url=account["bark_push"], notify_timeout=5))
        result = channel.send(f"什么值得买 · {result_text}", f"{account['label']} · {trigger}\n{message}")
    except Exception:
        return "Bark 推送失败"
    if result.success:
        return "Bark 已推送"
    return f"Bark 已推送 {result.delivered}/{result.total}" if result.delivered else "Bark 推送失败"


class CheckinService:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = directory / "checkin.sqlite3"
        self.lock = threading.Lock()
        self.running = set()
        self.executor = ThreadPoolExecutor(max_workers=1)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL,
                    cookie TEXT NOT NULL, sk TEXT NOT NULL, daily_time TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'auto',
                    bark_push TEXT NOT NULL DEFAULT '', bark_mode TEXT NOT NULL DEFAULT 'off'
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL
                        REFERENCES accounts(id) ON DELETE CASCADE,
                    started TEXT NOT NULL, source TEXT NOT NULL, message TEXT NOT NULL,
                    success INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_by_account ON runs(account_id, id DESC);
                CREATE TABLE IF NOT EXISTS schedule_state (
                    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                    last_attempt TEXT NOT NULL
                );
                INSERT INTO schedule_state(account_id,last_attempt)
                SELECT account_id,MAX(started) FROM runs WHERE source='auto' OR success=1
                GROUP BY account_id HAVING 1
                ON CONFLICT(account_id) DO UPDATE SET last_attempt=MAX(last_attempt,excluded.last_attempt);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(accounts)")}
            for name, default in (("platform", "auto"), ("bark_push", ""), ("bark_mode", "off")):
                if name not in columns:
                    db.execute(f"ALTER TABLE accounts ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
            if "task_options" not in columns:
                db.execute("ALTER TABLE accounts ADD COLUMN task_options TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self):
        with self.lock, self.connect() as db:
            accounts = []
            for row in db.execute("SELECT id, label, daily_time, platform, cookie, sk, bark_push, bark_mode, task_options FROM accounts ORDER BY id"):
                accounts.append({
                    "id": row["id"], "label": row["label"], "daily_time": row["daily_time"],
                    "platform": row["platform"], "running": row["id"] in self.running,
                    "bark_mode": row["bark_mode"],
                    "cookie": row["cookie"], "sk": row["sk"], "bark_push": row["bark_push"],
                    "task_options": TASK_DEFAULTS | json.loads(row["task_options"]),
                    "detected_platform": device_platform(parse_cookie_header(row["cookie"]), row["platform"]),
                })
            history = [dict(row) for row in db.execute("""
                SELECT label, started, source, message, success FROM runs
                JOIN accounts ON accounts.id = runs.account_id ORDER BY runs.id DESC LIMIT 50
            """)]
        return {"accounts": accounts, "history": history}

    def save(self, payload):
        label = payload.get("label", "")
        cookie = payload.get("cookie", "")
        sk = payload.get("sk", "")
        daily_time = payload.get("daily_time", "")
        platform = payload.get("platform", "auto")
        bark_push = payload.get("bark_push", "")
        bark_mode = payload.get("bark_mode", "off")
        account_id = payload.get("id")
        task_options = payload.get("task_options")
        if task_options is not None:
            if (not isinstance(task_options, dict) or task_options.keys() - TASK_DEFAULTS.keys()
                    or any(type(value) is not bool for value in task_options.values())):
                raise ValueError("任务开关格式无效")
            task_options = json.dumps(TASK_DEFAULTS | task_options)
        if not all(isinstance(value, str) for value in (label, cookie, sk, daily_time, platform, bark_push, bark_mode)):
            raise ValueError("配置格式无效")
        if bark_mode not in {"off", "all", "failure"}:
            raise ValueError("请选择有效的 Bark 通知选项")
        bark_push = validate_bark(bark_push) if bark_mode != "off" else ""
        if platform not in {"auto", "iphone", "android"}:
            raise ValueError("请选择有效的设备类型")
        label, cookie, sk = label.strip(), cookie.strip(), sk.strip()
        if not 1 <= len(label) <= 40:
            raise ValueError("请输入 1 至 40 字的账号名称")
        if daily_time and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", daily_time):
            raise ValueError("请选择有效的北京时间")
        if len(cookie) > 16000 or len(sk) > 1024 or re.search(r"[\x00-\x1f\x7f]", cookie + sk):
            raise ValueError("Cookie 或 SK 格式无效")
        if cookie:
            cookie = normalize_cookie(cookie)
        with self.lock, self.connect() as db:
            if account_id is None:
                if not cookie:
                    raise ValueError("请输入 App Cookie")
                if bark_mode != "off" and not bark_push:
                    raise ValueError("请输入 Bark 设备 Key 或推送地址")
                db.execute("INSERT INTO accounts(label,cookie,sk,daily_time,platform,bark_push,bark_mode,task_options) VALUES (?,?,?,?,?,?,?,?)",
                           (label, cookie, sk, daily_time, platform, bark_push, bark_mode, task_options or '{}'))
                return
            self.ensure_idle(account_id)
            previous = db.execute("SELECT cookie,sk,bark_push,task_options FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not previous:
                raise ValueError("账号已删除")
            if bark_mode != "off":
                bark_push = bark_push or previous["bark_push"]
                if not bark_push:
                    raise ValueError("请输入 Bark 设备 Key 或推送地址")
            db.execute("UPDATE accounts SET label=?,cookie=?,sk=?,daily_time=?,platform=?,bark_push=?,bark_mode=?,task_options=? WHERE id=?",
                       (label, cookie or previous["cookie"], sk if cookie or sk else previous["sk"],
                        daily_time, platform, bark_push, bark_mode, task_options or previous["task_options"], account_id))

    def ensure_idle(self, account_id):
        if type(account_id) is not int or not 1 <= account_id <= 2**63 - 1:
            raise ValueError("账号无效")
        if account_id in self.running:
            raise ValueError("该账号已有签到任务")

    def remove(self, account_id):
        with self.lock, self.connect() as db:
            self.ensure_idle(account_id)
            db.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def clear_history(self):
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM runs")

    def submit(self, account_id, source):
        with self.lock, self.connect() as db:
            self.ensure_idle(account_id)
            account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not account:
                raise ValueError("账号已删除")
            if source == "auto":
                now = datetime.now(SHANGHAI)
                if not account["daily_time"] or account["daily_time"] > now.strftime("%H:%M"):
                    return
                attempted = db.execute("""
                    SELECT 1 FROM schedule_state WHERE account_id=? AND last_attempt>=?
                """, (account_id, now.date().isoformat())).fetchone()
                if attempted:
                    return
            self.running.add(account_id)
            self.executor.submit(self.run, dict(account), source)

    def run(self, account, source):
        account_id = account["id"]
        started = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        try:
            try:
                message, success = checkin(account)
            except Exception as error:
                message, success = f"签到响应处理失败（{type(error).__name__}）", False
            with self.connect() as db:
                run = db.execute("INSERT INTO runs(account_id,started,source,message,success) VALUES(?,?,?,?,?)",
                                 (account_id, started, source, message, success))
                run_id = run.lastrowid
                if source == "auto" or success:
                    db.execute("""
                        INSERT INTO schedule_state(account_id,last_attempt) VALUES(?,?)
                        ON CONFLICT(account_id) DO UPDATE SET last_attempt=MAX(last_attempt,excluded.last_attempt)
                    """, (account_id, started))
                db.execute("""
                    DELETE FROM runs WHERE account_id=? AND id NOT IN (
                        SELECT id FROM runs WHERE account_id=? ORDER BY id DESC LIMIT 100
                    )
                """, (account_id, account_id))
            notification = push_bark(account, source, message, success)
            if notification:
                with self.connect() as db:
                    db.execute("UPDATE runs SET message=? WHERE id=?", (f"{message} · {notification}", run_id))
        except sqlite3.Error:
            print("Unable to save check-in result", file=sys.stderr, flush=True)
        finally:
            with self.lock:
                self.running.discard(account_id)

    def schedule(self):
        while True:
            try:
                with self.connect() as db:
                    accounts = list(db.execute("SELECT id FROM accounts WHERE daily_time<>''"))
                for account in accounts:
                    try:
                        self.submit(account["id"], "auto")
                    except ValueError:
                        continue
            except sqlite3.Error:
                print("Unable to read check-in schedule", file=sys.stderr, flush=True)
            threading.Event().wait(20)


class Handler(BaseHTTPRequestHandler):
    def address_string(self):
        return "fnOS gateway"

    def log_message(self, *_):
        pass

    def reply(self, status, content, mime="application/json; charset=utf-8"):
        if isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(content)

    def route(self):
        if self.headers.get("X-Trim-Isadmin", "").lower() != "true" or not self.headers.get("X-Trim-Userid", "").isdigit():
            self.reply(403, {"error": "请通过飞牛桌面以管理员身份访问"})
            return None
        path = urlsplit(self.path).path
        if path != PREFIX and not path.startswith(PREFIX + "/"):
            self.reply(404, {"error": "页面不存在"})
            return None
        return path[len(PREFIX):]

    def do_GET(self):
        path = self.route()
        if path is None:
            return
        if path == "/api/state":
            self.reply(200, self.server.service.snapshot())
        elif path in ASSETS:
            filename, mime = ASSETS[path]
            self.reply(200, (UI / filename).read_bytes(), mime)
        else:
            self.reply(404, {"error": "页面不存在"})

    def do_POST(self):
        path = self.route()
        if path is None:
            return
        if self.headers.get("X-SMZDM-Request") != "1" or self.headers.get_content_type() != "application/json":
            self.reply(403, {"error": "请求来源无效"})
            return
        service = self.server.service
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 32768:
                raise ValueError("请求大小无效")
            try:
                payload = json.loads(self.rfile.read(size))
            except (ValueError, UnicodeError):
                raise ValueError("请求格式无效") from None
            if not isinstance(payload, dict):
                raise ValueError("请求格式无效")
            if path == "/api/accounts/save":
                service.save(payload)
            elif path == "/api/accounts/remove":
                service.remove(payload.get("id"))
            elif path == "/api/checkin":
                service.submit(payload.get("id"), "manual")
            elif path == "/api/history/clear":
                service.clear_history()
            else:
                self.reply(404, {"error": "页面不存在"})
                return
        except ValueError as error:
            self.reply(400, {"error": str(error)})
        except sqlite3.Error:
            self.reply(500, {"error": "应用无法写入数据，请查看服务日志"})
        else:
            self.reply(200, {"ok": True})


def serve(directory):
    class GatewayServer(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

        def get_request(self):
            request, address = super().get_request()
            request.settimeout(10)
            return request, address

    os.umask(0o077)
    logger.remove()
    service = CheckinService(directory)
    with GatewayServer("app.sock", Handler) as server:
        server.service = service
        os.chmod("app.sock", 0o660)
        threading.Thread(target=service.schedule, daemon=True).start()
        server.serve_forever()


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
