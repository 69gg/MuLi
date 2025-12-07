"""
时间工具 - 提供全面的日期时间处理功能
包括时区支持、时间计算、格式化、计时器等功能
"""

## -!- START REGISTER TOOL -!- ##
## -!- START TOOL DEFINITION -!- ##
TOOL_NAME = "time"
TOOL_DESCRIPTION = "全面的时间日期处理工具，支持获取当前时间、时区转换、时间计算、格式化、计时器和秒表功能。每次开始对话你都应该使用他来获取当前时间和日期。"
TOOL_FUNCTIONS = [
    "get_current_time",
    "get_time_with_timezone",
    "convert_timezone",
    "add_time",
    "format_datetime",
    "timestamp_to_datetime",
    "datetime_to_timestamp",
    "start_timer",
    "stop_timer",
    "start_stopwatch",
    "stop_stopwatch",
    "get_time_difference"
]
TOOL_PARAMETERS = [
    [],
    [{"timezone": "时区名称，如 'Asia/Shanghai', 'America/New_York'"}],
    [
        {"datetime_str": "要转换的日期时间字符串 (格式: YYYY-MM-DD HH:MM:SS)"},
        {"from_timezone": "源时区名称"},
        {"to_timezone": "目标时区名称"}
    ],
    [
        {"base_datetime": "基准日期时间字符串 (格式: YYYY-MM-DD HH:MM:SS)"},
        {"days": "要添加的天数 (可以是负数)"},
        {"hours": "要添加的小时数 (可以是负数)"},
        {"minutes": "要添加的分钟数 (可以是负数)"},
        {"seconds": "要添加的秒数 (可以是负数)"}
    ],
    [
        {"datetime_str": "要格式化的日期时间字符串"},
        {"format_str": "格式化字符串 (如 '%Y-%m-%d %H:%M:%S', '%A, %B %d, %Y')"}
    ],
    [{"timestamp": "Unix时间戳 (秒)"}],
    [{"datetime_str": "日期时间字符串 (格式: YYYY-MM-DD HH:MM:SS)"}],
    [{"timer_name": "计时器名称 (可选，用于区分多个计时器)"}],
    [{"timer_name": "计时器名称 (可选)"}],
    [{"stopwatch_name": "秒表名称 (可选)"}],
    [{"stopwatch_name": "秒表名称 (可选)"}],
    [
        {"start_time": "开始时间字符串 (格式: YYYY-MM-DD HH:MM:SS)"},
        {"end_time": "结束时间字符串 (格式: YYYY-MM-DD HH:MM:SS)"}
    ]
]
## -!- END TOOL DEFINITION -!- ##

from datetime import datetime, timedelta
import time
from typing import Optional, Dict, Any
import json


# 计时器和秒表存储（内存中）
_timers: Dict[str, Dict[str, Any]] = {}
_stopwatches: Dict[str, Dict[str, Any]] = {}


def get_current_time() -> str:
    """
    获取当前本地时间

    返回:
        格式为 "YYYY-MM-DD HH:MM:SS" 的当前时间字符串
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_time_with_timezone(timezone: str = "Asia/Shanghai") -> str:
    """
    获取指定时区的当前时间

    参数:
        timezone: 时区名称 (如 'Asia/Shanghai', 'America/New_York', 'UTC')
                 常用时区: Asia/Shanghai, Asia/Tokyo, Asia/Hong_Kong,
                         America/New_York, America/Los_Angeles,
                         Europe/London, Europe/Paris, UTC

    返回:
        格式为 "YYYY-MM-DD HH:MM:SS TZ" 的时间字符串
    """
    try:
        import pytz
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        return now.strftime("%Y-%m-%d %H:%M:%S %Z")
    except ImportError:
        return f"错误：需要安装 pytz 库才能使用时区功能。当前本地时间: {get_current_time()}"
    except Exception as e:
        return f"错误：无效的时区 '{timezone}'。{str(e)}"


def convert_timezone(
    datetime_str: str,
    from_timezone: str,
    to_timezone: str
) -> str:
    """
    将日期时间从一个时区转换到另一个时区

    参数:
        datetime_str: 日期时间字符串，格式 "YYYY-MM-DD HH:MM:SS"
        from_timezone: 源时区名称
        to_timezone: 目标时区名称

    返回:
        转换后的日期时间字符串

    示例:
        convert_timezone("2025-12-07 15:30:00", "Asia/Shanghai", "America/New_York")
    """
    try:
        import pytz
        from_timezone_obj = pytz.timezone(from_timezone)
        to_timezone_obj = pytz.timezone(to_timezone)

        # 解析日期时间字符串
        dt = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M:%S")
        dt = from_timezone_obj.localize(dt)

        # 转换时区
        dt_converted = dt.astimezone(to_timezone_obj)

        return dt_converted.strftime("%Y-%m-%d %H:%M:%S %Z")
    except ImportError:
        return "错误：需要安装 pytz 库才能使用时区功能"
    except Exception as e:
        return f"错误：{str(e)}"


def add_time(
    base_datetime: str,
    days: int = 0,
    hours: int = 0,
    minutes: int = 0,
    seconds: int = 0
) -> str:
    """
    在基准日期时间上添加或减去指定的时间

    参数:
        base_datetime: 基准日期时间字符串，格式 "YYYY-MM-DD HH:MM:SS"
        days: 天数 (正数添加，负数减去)
        hours: 小时数
        minutes: 分钟数
        seconds: 秒数

    返回:
        计算后的日期时间字符串

    示例:
        add_time("2025-12-07 10:00:00", days=1, hours=2)  # 添加1天2小时
        add_time("2025-12-07 10:00:00", days=-1)  # 减去1天
    """
    try:
        dt = datetime.strptime(base_datetime, "%Y-%m-%d %H:%M:%S")
        delta = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        new_dt = dt + delta
        return new_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        return f"错误：{str(e)}"


def format_datetime(datetime_str: str, format_str: str) -> str:
    """
    将日期时间字符串格式化为指定格式

    参数:
        datetime_str: 日期时间字符串，格式 "YYYY-MM-DD HH:MM:SS"
        format_str: 格式化字符串
                   常用格式:
                   - "%Y-%m-%d %H:%M:%S" -> "2025-12-07 15:30:45"
                   - "%A, %B %d, %Y" -> "Sunday, December 07, 2025"
                   - "%Y年%m月%d日 %H时%M分%S秒" -> "2025年12月07日 15时30分45秒"
                   - "%I:%M %p" -> "03:30 PM"

    返回:
        格式化后的日期时间字符串
    """
    try:
        dt = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime(format_str)
    except Exception as e:
        return f"错误：{str(e)}"


def timestamp_to_datetime(timestamp: float) -> str:
    """
    将Unix时间戳转换为日期时间字符串

    参数:
        timestamp: Unix时间戳（秒）

    返回:
        格式为 "YYYY-MM-DD HH:MM:SS" 的日期时间字符串
    """
    try:
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        return f"错误：{str(e)}"


def datetime_to_timestamp(datetime_str: str) -> float:
    """
    将日期时间字符串转换为Unix时间戳

    参数:
        datetime_str: 日期时间字符串，格式 "YYYY-MM-DD HH:MM:SS"

    返回:
        Unix时间戳（秒）
    """
    try:
        dt = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M:%S")
        return dt.timestamp()
    except Exception as e:
        return -1.0


def start_timer(seconds: int, timer_name: str = "default") -> str:
    """
    启动一个倒计时计时器

    参数:
        seconds: 计时器时长（秒）
        timer_name: 计时器名称（可选，用于区分多个计时器）

    返回:
        计时器启动成功的确认信息
    """
    if timer_name in _timers and not _timers[timer_name].get("finished", True):
        return f"错误：计时器 '{timer_name}' 已在运行中"

    end_time = time.time() + seconds
    _timers[timer_name] = {
        "start_time": time.time(),
        "end_time": end_time,
        "duration": seconds,
        "finished": False
    }

    return f"计时器 '{timer_name}' 已启动: {seconds} 秒"


def stop_timer(timer_name: str = "default") -> str:
    """
    停止计时器并返回剩余时间或已过时间

    参数:
        timer_name: 计时器名称

    返回:
        计时器状态信息
    """
    if timer_name not in _timers:
        return f"错误：计时器 '{timer_name}' 不存在"

    timer_info = _timers[timer_name]
    current_time = time.time()

    if current_time >= timer_info["end_time"]:
        _timers[timer_name]["finished"] = True
        elapsed = current_time - timer_info["end_time"]
        return f"计时器 '{timer_name}' 已在 {elapsed:.1f} 秒前结束"
    else:
        remaining = timer_info["end_time"] - current_time
        _timers[timer_name]["finished"] = True
        return f"计时器 '{timer_name}' 已停止，剩余 {remaining:.1f} 秒"


def start_stopwatch(stopwatch_name: str = "default") -> str:
    """
    启动一个秒表（用于计时经过的时间）

    参数:
        stopwatch_name: 秒表名称（可选）

    返回:
        秒表启动确认信息
    """
    if stopwatch_name in _stopwatches:
        return f"秒表 '{stopwatch_name}' 已在运行中"

    _stopwatches[stopwatch_name] = {
        "start_time": time.time(),
        "running": True
    }

    return f"秒表 '{stopwatch_name}' 已启动"


def stop_stopwatch(stopwatch_name: str = "default") -> str:
    """
    停止秒表并返回经过的时间

    参数:
        stopwatch_name: 秒表名称

    返回:
        经过的时间（秒）
    """
    if stopwatch_name not in _stopwatches:
        return f"错误：秒表 '{stopwatch_name}' 不存在"

    stopwatch_info = _stopwatches[stopwatch_name]

    if not stopwatch_info["running"]:
        return f"秒表 '{stopwatch_name}' 已停止"

    elapsed = time.time() - stopwatch_info["start_time"]
    _stopwatches[stopwatch_name]["running"] = False

    hours = int(elapsed // 3600)
    minutes = int((elapsed % 3600) // 60)
    seconds = elapsed % 60

    if hours > 0:
        return f"秒表 '{stopwatch_name}' 停止: {hours}小时 {minutes}分 {seconds:.2f}秒"
    elif minutes > 0:
        return f"秒表 '{stopwatch_name}' 停止: {minutes}分 {seconds:.2f}秒"
    else:
        return f"秒表 '{stopwatch_name}' 停止: {seconds:.2f}秒"


def get_time_difference(start_time: str, end_time: str) -> str:
    """
    计算两个时间点之间的差值

    参数:
        start_time: 开始时间字符串，格式 "YYYY-MM-DD HH:MM:SS"
        end_time: 结束时间字符串，格式 "YYYY-MM-DD HH:MM:SS"

    返回:
        时间差描述
    """
    try:
        dt1 = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
        dt2 = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S")

        if dt2 < dt1:
            return "错误：结束时间不能早于开始时间"

        diff = dt2 - dt1
        days = diff.days
        hours = diff.seconds // 3600
        minutes = (diff.seconds % 3600) // 60
        seconds = diff.seconds % 60

        result = "时间差: "
        if days > 0:
            result += f"{days}天 "
        if hours > 0:
            result += f"{hours}小时 "
        if minutes > 0:
            result += f"{minutes}分钟 "
        if seconds > 0 or (days == 0 and hours == 0 and minutes == 0):
            result += f"{seconds}秒"

        # 总秒数
        total_seconds = int(diff.total_seconds())
        result += f" (总计 {total_seconds:,} 秒)"

        return result.strip()
    except Exception as e:
        return f"错误：{str(e)}"


# 清理已完成的计时器（可选的维护功能）
def _cleanup_timers():
    """清理已完成的计时器"""
    current_time = time.time()
    finished_timers = [
        name for name, info in _timers.items()
        if current_time >= info["end_time"] and not info.get("finished", False)
    ]
    for timer_name in finished_timers:
        _timers[timer_name]["finished"] = True


## -!- END REGISTER TOOL -!- ##

