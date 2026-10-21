"""Phase 94: 健康检查端口必须跟随 V4_PORT

update_version 的 app.py 监听 `V4_PORT`（默认 8002），但镜像里同时有一个
构建期常量 `ENV DSA_HEALTH_PORT=8002`。运行时用 `-e V4_PORT=9000` 换端口时：

  - 服务确实在 9000 上工作（实测 `/health` 返回 200）
  - 探针却一直打 8002，重试 3 次后容器被判 `unhealthy`

实测结果：容器 `Up About a minute (unhealthy)`，而 curl 18004 返回 200。
在有重启策略或编排器的环境里，一个完全健康的容器会被反复杀掉重建。

修复：`candidate_ports()` 把 `V4_PORT` 放在首位（主应用不设该变量，
对它是无害空操作）。必须排在 `DSA_HEALTH_PORT` 之前 —— 否则会先探旧端口，
若那里恰好有别的服务在监听就会误判 healthy。
"""
import importlib.util
import os
import sys

import pytest

HEALTHCHECK_PATH = os.path.join(
    # tests -> backend -> update_version -> 仓库根
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ),
    "docker",
    "healthcheck.py",
)


@pytest.fixture()
def hc(monkeypatch):
    """按文件路径加载共享的 healthcheck.py（它不是包内模块）"""
    spec = importlib.util.spec_from_file_location("dsa_healthcheck", HEALTHCHECK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 默认把所有端口来源清空，由各用例按需设置
    for key in ("V4_PORT", "DSA_HEALTH_PORT", "API_PORT", "DSA_HEALTH_PORT"):
        monkeypatch.delenv(key, raising=False)
    return mod


def test_v4_port_takes_precedence_over_stale_health_port(hc, monkeypatch):
    """运行时只改 V4_PORT 时，探针必须探新端口"""
    monkeypatch.setenv("V4_PORT", "9000")
    monkeypatch.setenv("DSA_HEALTH_PORT", "8002")  # 构建期遗留的旧值

    ports = hc.candidate_ports([])
    assert ports[0] == "9000", (
        f"探针会先探 {ports[0]}（stale），而不是 V4_PORT=9000。"
        f"完整顺序: {ports}"
    )
    assert "8002" in ports  # 旧值仍作为回退保留


def test_falls_back_to_builtin_default(hc):
    """一个端口来源都没有时回落到脚本内置默认值（主应用是 8000）

    update_version 镜像不依赖这个回退——它由 Dockerfile 的
    `ENV V4_PORT=8002` 显式提供端口，见上一条用例。
    """
    ports = hc.candidate_ports([])
    assert len(ports) == 1, f"应当只有一个候选端口: {ports}"
    assert ports[0] == hc.DEFAULT_PORT


def test_main_app_unaffected(hc, monkeypatch):
    """主应用不设 V4_PORT，行为不变：DSA_HEALTH_PORT 优先于 API_PORT/8000"""
    monkeypatch.setenv("DSA_HEALTH_PORT", "8123")
    monkeypatch.setenv("API_PORT", "8124")

    ports = hc.candidate_ports([])
    assert ports[0] == "8123", f"主镜像的端口优先级被改变了: {ports}"


def test_served_port_still_respected(hc, monkeypatch):
    """main.py --port N 仍然有效（排在环境变量之后）"""
    monkeypatch.setenv("V4_PORT", "9000")

    ports = hc.candidate_ports(["--port", "7777"])
    assert ports[0] == "9000"
    assert "7777" in ports
