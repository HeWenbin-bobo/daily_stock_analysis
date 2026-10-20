import os
import re
import subprocess
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_docker_entrypoint_has_valid_shell_syntax() -> None:
    subprocess.run(
        ["sh", "-n", str(REPO_ROOT / "docker" / "entrypoint.sh")],
        check=True,
    )


def test_dockerfile_uses_entrypoint_to_drop_privileges() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert "gosu" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]' in dockerfile
    assert "USER dsa" not in dockerfile


def test_dockerfile_bundles_builtin_screening_engine() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "screening.git" not in requirements.lower()
    assert "pip install -r requirements.txt" in dockerfile
    assert "--mount=type=cache,target=/root/.cache/pip" in dockerfile
    assert "import src.services.screening.pipeline" in dockerfile


def test_docker_entrypoint_repairs_ownership_and_user_permissions() -> None:
    entrypoint = (REPO_ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    assert "directory_needs_repair" in entrypoint
    assert "has_unwritable_mount_path" in entrypoint
    assert "can_write_dir_as_app_user" in entrypoint
    assert "DATABASE_FILE" in entrypoint
    assert "/home/dsa/.longbridge" in entrypoint
    assert 'HOME="/home/dsa"' in entrypoint
    assert re.search(r"export\s+HOME\s+exec\s+gosu", entrypoint, re.DOTALL)
    assert re.search(r"\bchown\s+-R\b", entrypoint)
    assert re.search(r"\bchmod\s+-R\s+u\+rwX\b", entrypoint)
    assert re.search(r"gosu\s+\"\$APP_USER:\$APP_GROUP\"\s+test\s+-w", entrypoint)


def test_docker_compose_injects_env_without_single_file_env_mount() -> None:
    compose_text = (REPO_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)
    common = compose["x-common"]

    assert "../.env" in common["env_file"]
    assert "../.env:/app/.env" not in common["volumes"]
    assert not any(str(volume).startswith("../.env:") for volume in common["volumes"])
    assert "../longbridge_tokens:/home/dsa/.longbridge" in common["volumes"]


def test_docker_compose_default_memory_recommendation_is_not_512m() -> None:
    compose_text = (REPO_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)
    resources = compose["x-common"]["deploy"]["resources"]

    assert resources["limits"]["memory"] == "1G"
    assert resources["reservations"]["memory"] == "512M"
    assert "512M" in compose_text
    assert "MAX_WORKERS=1" in compose_text


def test_docker_memory_guides_describe_resource_profiles() -> None:
    doc_paths = (
        "docs/DEPLOY.md",
        "docs/DEPLOY_EN.md",
        "docs/full-guide.md",
        "docs/full-guide_EN.md",
        "docs/docker/zeabur-deployment.md",
    )

    for doc_path in doc_paths:
        doc = (REPO_ROOT / doc_path).read_text(encoding="utf-8")

        assert "512M" in doc
        assert "1G" in doc
        assert "2G+" in doc
        assert "MAX_WORKERS=1" in doc


def test_docker_guides_do_not_recommend_single_file_env_bind_mount() -> None:
    forbidden_mount_patterns = [
        r"\$\(pwd\)/\.env:/app/\.env",
        r"\.\./\.env:/app/\.env",
    ]

    for doc_path in ("docs/full-guide.md", "docs/full-guide_EN.md"):
        doc = (REPO_ROOT / doc_path).read_text(encoding="utf-8")

        assert "--env-file .env" in doc
        assert "env_file:" in doc
        for pattern in forbidden_mount_patterns:
            assert re.search(pattern, doc) is None


def test_documented_compose_exec_commands_run_as_dsa() -> None:
    safe_exec_prefix = "docker-compose -f ./docker/docker-compose.yml exec -u dsa"
    unsafe_exec_prefix = "docker-compose -f ./docker/docker-compose.yml exec"

    for doc_path in ("docs/DEPLOY.md", "docs/DEPLOY_EN.md"):
        doc = (REPO_ROOT / doc_path).read_text(encoding="utf-8")

        assert f"{safe_exec_prefix} stock-analyzer bash" in doc
        assert f"{safe_exec_prefix} stock-analyzer python main.py --no-notify" in doc
        assert f"{unsafe_exec_prefix} stock-analyzer bash" not in doc
        assert (
            f"{unsafe_exec_prefix} stock-analyzer python main.py --no-notify"
            not in doc
        )


def _write_fake_command(fakebin: Path, name: str, body: str) -> None:
    command = fakebin / name
    command.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    command.chmod(0o755)


def _prepare_fake_entrypoint_tools(tmp_path: Path, find_body: str) -> tuple[Path, Path]:
    fakebin = tmp_path / "bin"
    log_dir = tmp_path / "logs"
    fakebin.mkdir()
    log_dir.mkdir()

    _write_fake_command(
        fakebin,
        "id",
        'if [ "${1:-}" = "-u" ]; then printf "0\\n"; else printf "0\\n"; fi\n',
    )
    _write_fake_command(fakebin, "mkdir", "exit 0\n")
    _write_fake_command(fakebin, "find", find_body)
    _write_fake_command(
        fakebin,
        "chown",
        'printf "%s\\n" "$*" >> "$FAKE_LOG_DIR/chown.log"\n'
        'exit "${CHOWN_EXIT:-0}"\n',
    )
    _write_fake_command(
        fakebin,
        "chmod",
        'printf "%s\\n" "$*" >> "$FAKE_LOG_DIR/chmod.log"\n'
        'exit "${CHMOD_EXIT:-0}"\n',
    )
    _write_fake_command(
        fakebin,
        "gosu",
        'shift\n'
        'case "$1" in\n'
        '    sh|test) exit "${GOSU_WRITE_EXIT:-0}" ;;\n'
        'esac\n'
        'exec "$@"\n',
    )

    return fakebin, log_dir


def _run_entrypoint_with_fake_tools(
    fakebin: Path,
    log_dir: Path,
    *,
    gosu_write_exit: int,
    chown_exit: int,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["FAKE_LOG_DIR"] = str(log_dir)
    env["GOSU_WRITE_EXIT"] = str(gosu_write_exit)
    env["CHOWN_EXIT"] = str(chown_exit)

    return subprocess.run(
        ["sh", str(REPO_ROOT / "docker" / "entrypoint.sh"), "true"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def test_docker_entrypoint_repairs_nested_mount_ownership(tmp_path: Path) -> None:
    fakebin, log_dir = _prepare_fake_entrypoint_tools(
        tmp_path,
        'for arg in "$@"; do\n'
        '    if [ "$arg" = "-maxdepth" ]; then exit 0; fi\n'
        "done\n"
        'printf "%s/nested-root-owned\\n" "$1"\n',
    )

    _run_entrypoint_with_fake_tools(
        fakebin,
        log_dir,
        gosu_write_exit=0,
        chown_exit=0,
    )

    chown_log = (log_dir / "chown.log").read_text(encoding="utf-8")
    chmod_log = (log_dir / "chmod.log").read_text(encoding="utf-8")
    assert "/app/data" in chown_log
    assert "/app/logs" in chown_log
    assert "/app/reports" in chown_log
    assert "/app/data" in chmod_log


def test_docker_entrypoint_skips_owner_chmod_when_chown_fails(tmp_path: Path) -> None:
    fakebin, log_dir = _prepare_fake_entrypoint_tools(
        tmp_path,
        'printf "%s/root-owned\\n" "$1"\n',
    )

    result = _run_entrypoint_with_fake_tools(
        fakebin,
        log_dir,
        gosu_write_exit=1,
        chown_exit=1,
    )

    assert (log_dir / "chown.log").exists()
    assert not (log_dir / "chmod.log").exists()
    assert "skipping owner-only chmod" in result.stderr
    assert "still not writable by dsa" in result.stderr


# ============================================================================
# update_version (v2) 镜像
# ============================================================================

def _v2_compose() -> dict:
    text = (REPO_ROOT / "update_version" / "docker-compose.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def test_v2_dockerfile_keeps_repo_layout_for_main_root_imports() -> None:
    """update_version 反推仓库根来 import 主项目模块，层级错一位就起不来。"""
    dockerfile = (REPO_ROOT / "update_version" / "Dockerfile").read_text(encoding="utf-8")

    # backend/api/endpoints_hot.py 用 4 层 parent 定位仓库根，
    # 因此镜像里必须 /app/{src,api,data_provider} 与 /app/update_version/backend 并存。
    assert "COPY src/ ./src/" in dockerfile
    assert "COPY api/ ./api/" in dockerfile
    assert "COPY data_provider/ ./data_provider/" in dockerfile
    assert "COPY update_version/backend/ ./update_version/backend/" in dockerfile
    # bot/ 不是可选：src/notification.py 顶层 import bot.models，缺了它
    # /hot-analysis/preview、/run-async、PDF 导出整条链路直接 500。
    assert "COPY bot/ ./bot/" in dockerfile
    # strategies/ 是 agent 技能目录，缺失只会静默跳过技能分析。
    assert "COPY strategies/ ./strategies/" in dockerfile


def test_v2_dockerfile_installs_pdf_dependencies() -> None:
    """PDF 导出走主项目 reportlab 实现，字体路径写死为 wqy-zenhei。"""
    dockerfile = (REPO_ROOT / "update_version" / "Dockerfile").read_text(encoding="utf-8")

    assert "reportlab" in dockerfile
    assert "fonts-wqy-zenhei" in dockerfile


def test_v2_dockerfile_provides_main_project_runtime_dirs() -> None:
    """主项目模块按仓库根写 /app/{data,logs,reports}，不看 update_version 约定。"""
    dockerfile = (REPO_ROOT / "update_version" / "Dockerfile").read_text(encoding="utf-8")

    assert "/app/data /app/logs /app/reports" in dockerfile
    assert "/app/data" in dockerfile.split("DSA_WRITABLE_DIRS=")[1].split("\n")[0]


def test_v2_dockerfile_installs_optuna_because_startup_imports_it() -> None:
    """endpoints_v2 在模块级 import hyperopt_engine，后者顶层 import optuna。

    主项目 requirements.txt 不需要 optuna，所以它只能装在 v2 镜像里；
    漏装的表现是应用启动即 ImportError。
    """
    dockerfile = (REPO_ROOT / "update_version" / "Dockerfile").read_text(encoding="utf-8")
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "optuna" in dockerfile
    assert "optuna" not in requirements.lower()


def test_v2_dockerfile_does_not_bake_credentials_or_databases() -> None:
    """backend/data 存 .encryption_key 与 SQLite 库，绝不能被 COPY 进镜像层。"""
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "update_version/backend/data/" in dockerignore
    assert "update_version/data/" in dockerignore
    assert "update_version/frontend/node_modules/" in dockerignore
    # 整目录排除会连源码一起丢掉
    assert not re.search(r"^update_version/$", dockerignore, re.MULTILINE)


def test_v2_compose_mounts_persistent_state_and_env() -> None:
    compose = _v2_compose()
    service = compose["services"]["v2"]

    assert "../.env" in service["env_file"]
    assert not any(str(volume).startswith("../.env:") for volume in service["volumes"])
    volumes = service["volumes"]
    assert "./backend/data:/app/update_version/backend/data" in volumes
    assert "./data:/app/update_version/data" in volumes
    # 主项目模块（PortfolioService / llm usage / ETF 报告）按仓库根写路径，
    # 不挂载时组合账户、交易、用量与报告在容器重建后全部丢失。
    assert "./data/main:/app/data" in volumes
    assert "./reports:/app/reports" in volumes
    assert service["environment"]
    assert "DATABASE_PATH=/app/data/stock_analysis.db" in service["environment"]


def test_v2_compose_health_probe_matches_its_own_endpoint() -> None:
    """v2 没有 /api/health，探针必须指向 /health，否则容器永远 unhealthy。"""
    dockerfile = (REPO_ROOT / "update_version" / "Dockerfile").read_text(encoding="utf-8")

    assert "DSA_HEALTH_PATH=/health" in dockerfile
    assert "DSA_HEALTH_PORT=8002" in dockerfile


def test_v2_compose_allows_time_for_cron_shutdown() -> None:
    compose = _v2_compose()
    service = compose["services"]["v2"]

    # Docker 默认只等 10s；lifespan 里要关 APScheduler，给足余量。
    assert service["stop_grace_period"] != "10s"
    assert "stop_grace_period" in service


def test_v2_app_serves_frontend_and_respects_port_env() -> None:
    app_py = (REPO_ROOT / "update_version" / "backend" / "app.py").read_text(encoding="utf-8")

    # 没有静态托管时只能靠 vite dev server，无法作为容器对外提供服务。
    assert "_mount_frontend" in app_py
    assert "V4_PORT" in app_py
