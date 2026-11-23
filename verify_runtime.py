"""Verify the generated FPK and exercise its packaged Linux runtime with fake responses."""

from contextlib import closing
import io
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qs

from build_fpk import ROOT, verify


def main():
    packages = list((ROOT / "dist").glob("*.fpk"))
    if len(packages) != 1:
        raise RuntimeError("Expected one freshly built FPK in dist")
    package = packages[0]
    verify(package)
    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary)
        with tarfile.open(package) as outer:
            data = outer.extractfile("app.tgz").read()
        with tarfile.open(fileobj=io.BytesIO(data)) as inner:
            inner.extractall(target / "app", filter="data")
        sys.path[:0] = [str(target / "app"), str(target / "app/vendor")]

        import httpx
        from service import CheckinService, checkin, push_bark, run_optional_tasks
        from smzdm_bot.exceptions import APIError
        from smzdm_bot.models import LotteryResult, TaskReport, TaskItemResult, TaskOutcome
        from smzdm_bot.protocol import generate_security_key

        expected_sk = generate_security_key("123", "demo-device")
        for platform in ("android", "iphone"):
            cookie = f"sess=demo; smzdm_id=123; device_id=demo-device; device_smzdm={platform}"

            def response(request):
                form = parse_qs(request.content.decode())
                assert form["f"] == [platform] and "sign" in form
                assert form.get("sk") == ([expected_sk] if platform == "android" else None)
                data = {"daily_num": 5} if request.url.path == "/checkin" else {"rows": []}
                return httpx.Response(200, json={"error_code": 0, "data": data})

            with patch("smzdm_bot.client.httpx.Client", return_value=httpx.Client(transport=httpx.MockTransport(response))):
                assert checkin({"cookie": cookie, "platform": platform, "sk": ""})[1]
        with patch("httpx.post", return_value=httpx.Response(200, json={"code": 200})):
            assert push_bark({"bark_mode": "all", "bark_push": "demo", "label": "Demo"}, "auto", "ok", True) == "Bark 已推送"

        report = TaskReport("每日任务", [TaskItemResult("浏览文章", TaskOutcome.COMPLETED)])
        options = {"extra_rewards": True, "daily": True, "lottery": True, "follow": False}
        order = []
        with patch("service.wait_for_random_delay"), \
                patch("service.claim_extra_checkin_reward", side_effect=lambda _: order.append("reward") or True), \
                patch("service.DailyTaskExecutor") as daily, \
                patch("service.perform_task_lottery", side_effect=lambda _: order.append("lottery") or LotteryResult(success=True, message="奖品")), \
                patch("service.enter_free_crowd_lotteries", side_effect=lambda _: order.append("crowd") or 2):
            daily.return_value.execute.side_effect = lambda: order.append("daily") or report
            lines = run_optional_tasks(object(), options)
            assert order == ["reward", "daily", "lottery", "crowd"]
            assert daily.call_args.args[1].enable_follow_tasks is False
            assert "2" in lines[-1] and "奖品" in lines[-2]
            assert run_optional_tasks(object(), dict.fromkeys(options, False)) == []

        with patch("service.wait_for_random_delay"), \
                patch("service.perform_daily_checkin", side_effect=APIError("今日已签到", error_code=4)), \
                patch("service.claim_extra_checkin_reward", return_value=True) as reward:
            message, success = checkin({"cookie": "sess=demo", "platform": "iphone", "sk": ""})
            assert success and "今日已签到" in message and "已领取" in message
            reward.assert_called_once()

        directory = target / "data"
        directory.mkdir()
        with closing(sqlite3.connect(directory / "checkin.sqlite3")) as db:
            db.executescript("""
                CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT,label TEXT,
                    cookie TEXT,sk TEXT,daily_time TEXT,platform TEXT);
                INSERT INTO accounts VALUES(1,'Demo','sess=demo','','08:30','iphone');
            """)
        service = CheckinService(directory)
        try:
            assert service.snapshot()["accounts"][0]["cookie"] == "sess=demo"
            assert service.snapshot()["accounts"][0]["task_options"]["daily"] is False
            service.save({"id": 1, "label": "Demo", "platform": "iphone", "bark_mode": "all", "bark_push": "demo", "task_options": options})
            assert service.snapshot()["accounts"][0]["task_options"] == options
            with service.connect() as db:
                account = dict(db.execute("SELECT * FROM accounts WHERE id=1").fetchone())
            with patch("service.checkin", return_value=("ok", True)), patch("service.push_bark", return_value="Bark 已推送"):
                service.run(account, "auto")
            assert service.snapshot()["history"][0]["success"] == 1
            service.clear_history()
            assert service.snapshot()["history"] == []
            with patch.object(service.executor, "submit") as submit:
                service.submit(1, "auto")
                submit.assert_not_called()
        finally:
            service.executor.shutdown()
    print("Packaged Linux runtime, Android/iPhone signatures, Bark and database migration passed")


if __name__ == "__main__":
    main()
