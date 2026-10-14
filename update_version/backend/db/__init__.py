# -*- coding: utf-8 -*-
"""backend/db 包：schema 迁移与建表。

★ 修复：此前缺少 __init__.py，`db` 退化为 namespace package；
  子进程（multiprocessing spawn / runpy 重导 app.py）中 sys.path
  含仓库内其他目录时，根目录的 `db/`（含 history.py，无 migrate）
  作为 regular package 抢先解析，导致 ModuleNotFoundError: db.migrate。
  显式声明为 regular package 后，app.py 将 backend 目录插到 sys.path[0]，
  本包优先于根目录 db/ 解析。
"""
