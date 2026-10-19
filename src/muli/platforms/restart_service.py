"""Restart the Windows task from an independent, single-use scheduled task."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from muli.platforms.service_runner import BoundedLog
from muli.process import run_process
from muli.service import TASK_NAME


async def restart(task_name: str) -> None:
    if re.fullmatch(r"MuLi-restart-[0-9a-f]{32}", task_name) is None:
        raise ValueError("重启助手任务名称无效")
    script = (
        "$s=New-Object -ComObject Schedule.Service;$s.Connect();"
        "[int]$s.GetFolder('\\').GetTask('MuLi').State"
    )
    try:
        status = await run_process(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=5
        )
        if status["exit_code"] or str(status["output"]).strip() not in {"2", "3", "4"}:
            raise ValueError("无法确认主服务任务状态")
        if str(status["output"]).strip() in {"2", "4"}:
            ended = await run_process(["schtasks", "/End", "/TN", TASK_NAME], timeout=10)
            if ended["exit_code"]:
                raise ValueError("无法停止主服务任务")
            async with asyncio.timeout(15):
                while True:
                    status = await run_process(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                        timeout=5,
                    )
                    if status["exit_code"] == 0 and str(status["output"]).strip() == "3":
                        break
                    await asyncio.sleep(0.2)
        started = await run_process(["schtasks", "/Run", "/TN", TASK_NAME], timeout=10)
        if started["exit_code"]:
            raise ValueError("无法重新启动主服务任务")
    finally:
        await run_process(["schtasks", "/Delete", "/TN", task_name, "/F"], timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--log-file", required=True, type=Path)
    arguments = parser.parse_args()
    log = BoundedLog(arguments.log_file)
    sys.stdout, sys.stderr = log, log
    asyncio.run(restart(arguments.task_name))


if __name__ == "__main__":
    main()
