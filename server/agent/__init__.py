# -*- coding: utf-8 -*-
"""AI Agent - NLP intent parsing & tool-calling layer."""
import importlib, sys, os

_main_module = None

_MAIN_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "main.py"))


def _import_main():
    """延迟导入 main 模块（避免循环依赖）。

    两种启动路径都要能被复用，否则同一份 main.py 会被**执行两次**，得到两个
    app 对象与两份模块级缓存（浪费、且有状态分叉风险）：
      * `python server/main.py` → 模块名是 ``__main__``；
      * 自测脚本里的 `import main` → 模块名是 ``main``。
    只有当 __file__ 确实指向本仓库的 server/main.py 时才复用。
    """
    global _main_module
    if _main_module is None:
        for key in ("main", "__main__"):
            cached = sys.modules.get(key)
            cached_file = getattr(cached, "__file__", None) if cached else None
            if cached_file and os.path.abspath(cached_file) == _MAIN_FILE:
                _main_module = cached
                return _main_module
        spec = importlib.util.spec_from_file_location("main", _MAIN_FILE)
        _main_module = importlib.util.module_from_spec(spec)
        sys.modules["main"] = _main_module
        spec.loader.exec_module(_main_module)
    return _main_module