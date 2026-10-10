"""统一的超时重试；HTTP 状态及非超时连接错误不重试。"""
import math
import time

import requests
from urllib3.exceptions import ReadTimeoutError

from conf.config import crackConfig
import logs.log as Log


class TaskStopped(requests.exceptions.RequestException):
    """任务级 stop 事件已置位：本次请求被主动取消，不是站点请求失败。"""


def is_timeout(exc):
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    # requests 在读取响应体时会把 urllib3 ReadTimeoutError 包装为 ConnectionError。
    if not isinstance(exc, requests.exceptions.ConnectionError):
        return False
    pending, seen = [exc], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ReadTimeoutError):
            return True
        pending.extend(value for value in getattr(current, 'args', ()) if isinstance(value, BaseException))
        pending.extend(value for value in (current.__cause__, current.__context__) if value is not None)
    return False


def request_with_timeout_retries(operation, *, context='HTTP 请求', stop_event=None):
    """首次尝试之外，默认对超时额外重试三次；operation 包含完整响应体读取。"""
    retries = crackConfig.get('timeout_retries', 3)
    delay = crackConfig.get('timeout_retry_delay', .3)
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError('timeout_retries 必须为非负整数')
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0:
        raise ValueError('timeout_retry_delay 必须为有限非负数')
    for attempt in range(retries + 1):
        if stop_event is not None and stop_event.is_set():
            raise TaskStopped('任务已停止，取消请求及重试')
        try:
            return operation()
        except requests.RequestException as exc:
            if not is_timeout(exc) or attempt == retries:
                raise
            if stop_event is not None and stop_event.is_set():
                raise TaskStopped('任务已停止，取消请求及重试') from exc
            Log.Info(f'[*] {context} 超时，重试 ({attempt + 1}/{retries})')
            if stop_event is not None:
                if stop_event.wait(delay):
                    raise TaskStopped('任务已停止，取消请求及重试') from exc
            elif delay:
                time.sleep(delay)
