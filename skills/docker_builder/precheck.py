"""
Docker Builder 预检模块（Windows 版）
职责：检查 Docker 环境、项目路径、依赖文件、挂载可行性。
"""

import os
import subprocess


def run_precheck(state: dict) -> dict:
    issues = []
    updates = {}

    # 1. 提取项目路径
    project_path = state.get("project_path_win", "").strip()
    if not project_path:
        project_path = state.get("project_path_wsl", "").strip()
    
    if not project_path:
        issues.append("project_path_win 为空，未获取到项目路径")
        return {"precheck_ok": False, "issues": issues, "updates": updates}

    updates["project_path_win"] = project_path

    # 2. 项目路径有效性
    if not os.path.exists(project_path):
        issues.append(f"项目路径不存在: {project_path}")
        return {"precheck_ok": False, "issues": issues, "updates": updates}
    
    if not os.path.isdir(project_path):
        issues.append(f"项目路径不是目录: {project_path}")
        return {"precheck_ok": False, "issues": issues, "updates": updates}

    # 3. requirements 文件存在性
    requirements_path = state.get("requirements_path", "").strip()
    if not requirements_path:
        # 尝试默认路径
        requirements_path = os.path.join(project_path, "requirements_agent.txt")
    
    if not os.path.exists(requirements_path):
        issues.append(f"未找到依赖文件: {requirements_path}")
    else:
        updates["requirements_path"] = requirements_path

    # 4. Docker 安装检查
    try:
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            issues.append("Docker 未安装或未加入 PATH")
    except Exception:
        issues.append("无法执行 docker 命令，请确认 Docker Desktop 已安装")

    # 5. Docker 守护进程检查
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            issues.append("Docker 守护进程未运行，请启动 Docker Desktop")
    except Exception:
        issues.append("无法连接 Docker 守护进程")

    # 6. 挂载可行性预检（关键：验证 Docker 能否访问该 Windows 路径）
    if not issues:
        try:
            test_result = subprocess.run(
                ["docker", "run", "--rm", "-v", f"{project_path}:/app-test",
                 "alpine", "ls", "/app-test"],
                capture_output=True, text=True, timeout=30
            )
            if test_result.returncode != 0:
                err = test_result.stderr[:200]
                issues.append(f"Docker 挂载测试失败: {err}。请检查 Docker Desktop → Settings → Resources → File sharing")
        except Exception as e:
            issues.append(f"Docker 挂载预检异常: {e}")

    # 7. 镜像名/容器名：不再硬编码默认值
    # 让 docker_builder_agent.py 的 call_model 根据 project_path 动态推断
    # 仅在用户已显式指定时保留
    if state.get("docker_image"):
        updates["docker_image"] = state.get("docker_image")
    if state.get("container_name"):
        updates["container_name"] = state.get("container_name")

    # 只有非挂载类致命错误才阻断；挂载问题也阻断但提示更明确
    real_issues = [i for i in issues if "请检查" not in i or "守护进程" in i]
    ok = len(real_issues) == 0 and len(issues) == 0

    return {
        "precheck_ok": ok,
        "issues": issues,
        "updates": updates
    }