import random
from functools import lru_cache

from fake_useragent import UserAgent
from conf.config import generatorConfig


@lru_cache(maxsize=1)
def _user_agent():
    return UserAgent()


def get_random_headers():
    """返回独立的请求头，关闭随机化时使用配置中的默认值。"""
    config = generatorConfig["headers_config"]
    headers = config["default_headers"].copy()
    if config["enable"]:
        headers["User-Agent"] = _user_agent().random
    return headers
