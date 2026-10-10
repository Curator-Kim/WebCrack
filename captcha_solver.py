"""可选验证码识别适配层。

仅在运行环境安装了 ddddocr 时提供识别能力；依赖缺失或模型加载失败时
`available()` 返回 False，由调用方决定降级行为（跳过需要验证码的网站）。
模块不在导入时加载 ddddocr，避免未使用验证码识别时承担加载开销。
"""
import base64
import threading

__all__ = ["available", "recognize", "decode_data_uri", "reset"]

_LOCK = threading.RLock()
_LOAD_DONE = False
_MODULE = None
_ENGINE = None


def _module():
    """首次调用时尝试导入 ddddocr；结果缓存，导入失败不抛出。"""
    global _LOAD_DONE, _MODULE
    if _LOAD_DONE:
        return _MODULE
    with _LOCK:
        if not _LOAD_DONE:
            try:
                import ddddocr as module
            except Exception:  # 依赖缺失、ABI 不匹配或平台不支持
                module = None
            _MODULE = module
            _LOAD_DONE = True
    return _MODULE


def _engine():
    """构造并复用识别引擎；构造失败视为环境不支持。"""
    global _ENGINE
    module = _module()
    if module is None:
        return None
    if _ENGINE is None:
        with _LOCK:
            if _ENGINE is None:
                try:
                    try:
                        _ENGINE = module.DdddOcr(show_ad=False)
                    except TypeError:  # 旧版本没有 show_ad 参数
                        _ENGINE = module.DdddOcr()
                except Exception:
                    _ENGINE = None
                    return None
    return _ENGINE


def available():
    """当前进程是否可用 ddddocr 识别验证码。"""
    return _engine() is not None


def recognize(image):
    """识别图片字节；返回去空白文本，识别为空或异常时返回 None。

    onnxruntime 会话不做并发保证，这里串行执行推理；图片获取仍可并发。
    """
    if not image or _module() is None:
        return None
    engine = _engine()
    if engine is None:
        return None
    with _LOCK:
        try:
            text = engine.classification(image)
        except Exception:
            return None
    if not isinstance(text, str):
        text = str(text)
    text = "".join(text.split())
    return text or None


def decode_data_uri(url):
    """解析 data: URI 中的图片字节；不是内联图片或解析失败时返回 None。"""
    if not isinstance(url, str) or not url.lower().startswith("data:"):
        return None
    if "," not in url:
        return None
    header, payload = url.split(",", 1)
    if ";base64" not in header.lower():
        return None
    try:
        return base64.b64decode(payload, validate=False)
    except Exception:
        return None


def reset():
    """清空导入与引擎缓存（供测试或运行期重新探测使用）。"""
    global _LOAD_DONE, _MODULE, _ENGINE
    with _LOCK:
        _LOAD_DONE = False
        _MODULE = None
        _ENGINE = None
