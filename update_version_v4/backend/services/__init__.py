# -*- coding: utf-8 -*-
"""backend/services 包：深度研究、Berkshire 等业务服务。

★ 修复：此前缺少 __init__.py，`services` 退化为 namespace package；
  spawn 子进程（重导 app.py）中 v4 根目录的 services/（regular package，
  仅含 market_review_service.py）抢先解析，导致
  ModuleNotFoundError: services.deep_research。
"""
