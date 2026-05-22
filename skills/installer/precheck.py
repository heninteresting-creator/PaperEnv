"""
Installer 预检模块 —— 纯 uv 路线（底层事实收集）
"""

import json
import re
import subprocess


def run_precheck(state: dict) -> dict:
    """
    收集容器状态事实。
    返回: {"precheck_ok": bool, "issues": [str], "warnings": [str], "updates": {str: any}}
    """
    issues = []      # 阻断性问题
    warnings = []    # 非阻断性问题
    updates = {}

    container_name = state.get("container_name", "").strip() or "dyfo-container"
    updates["container_name"] = container_name

    # 1. 容器存活（阻断）—— 用 inspect 精确查状态，避免 name 模糊匹配
    rc, out, err = _exec([
        "docker", "inspect", "-f", "{{.State.Status}}", container_name
    ], timeout=10)
    
    if rc != 0:
        issues.append(f"容器 {container_name} 不存在或无法访问: {err[:200]}")
        return {"precheck_ok": False, "issues": issues, "warnings": warnings, "updates": updates}
    
    if out.strip() != "running":
        issues.append(f"容器 {container_name} 状态为 {out.strip()}，未运行")
        return {"precheck_ok": False, "issues": issues, "warnings": warnings, "updates": updates}

    # 2. requirements 文件存在（阻断）
    req_path = "/workspace/requirements_agent.txt"
    rc, _, _ = _exec(["docker", "exec", container_name, "test", "-f", req_path], timeout=10)
    if rc != 0:
        issues.append(f"容器内未找到 requirements: {req_path}")
        return {"precheck_ok": False, "issues": issues, "warnings": warnings, "updates": updates}

    # 读取内容
    rc, content, _ = _exec(["docker", "exec", container_name, "cat", req_path], timeout=10)
    if rc != 0:
        issues.append("无法读取 requirements 内容")
        return {"precheck_ok": False, "issues": issues, "warnings": warnings, "updates": updates}

    # 解析包列表（忽略注释和空行）
    packages = []
    for line in content.split('\n'):
        line = line.strip()
        if line and not line.startswith('#'):
            packages.append(line)
    
    # 空文件不阻断，标记跳过安装
    if not packages:
        updates["skip_install"] = True
        updates["requirements_content"] = content
        updates["packages"] = []
        return {
            "precheck_ok": True,
            "issues": [],
            "warnings": ["requirements 文件为空，无需安装"],
            "updates": updates
        }

    updates["requirements_content"] = content
    updates["packages"] = packages

        # 1.5 读取 heavy.json（Scanner 2.1 输出，兜底）
    heavy_path = "/workspace/requirements_heavy.json"
    rc, heavy_content, _ = _exec(["docker", "exec", container_name, "cat", heavy_path], timeout=10)
    heavy_packages = []
    if rc == 0:
        try:
            heavy_packages = json.loads(heavy_content)
            print(f"[INSTALLER PRE] 读取 heavy.json: {len(heavy_packages)} 个 heavy 包")
        except Exception:
            pass
    updates["heavy_packages"] = heavy_packages

    # 3. 检测 torch CUDA 版本，推断 extra-index-url
    cuda_match = re.search(r'\+cu(\d{2,3})', content)
    if cuda_match:
        cuda_ver = cuda_match.group(1)
        extra_url = f"https://download.pytorch.org/whl/cu{cuda_ver}"
        updates["extra_index_url"] = extra_url
        print(f"[INSTALLER PRE] 检测到 CUDA cu{cuda_ver}, extra_index_url={extra_url}")
    elif re.search(r'^(?!#).*torch==', content, re.MULTILINE | re.IGNORECASE):
        # 有 torch 但无 +cu，走 CPU 版
        updates["extra_index_url"] = "https://download.pytorch.org/whl/cpu"
        print(f"[INSTALLER PRE] 有 torch 无 CUDA 标记，使用 CPU 版 index")

    # 4. 纯 uv 检查（阻断）：只认 uv
    rc, _, _ = _exec(["docker", "exec", container_name, "which", "uv"], timeout=10)
    if rc != 0:
        issues.append("容器内未找到 uv（纯 uv 路线要求 uv 必须预装）")
        return {"precheck_ok": False, "issues": issues, "warnings": warnings, "updates": updates}

    # 5. 网络检查（非阻断，只警告）—— 用 python 检查，精简镜像一定有 python
    rc, _, err = _exec([
        "docker", "exec", container_name,
        "python", "-c",
        "import urllib.request; urllib.request.urlopen('https://pypi.org', timeout=5)"
    ], timeout=15)
    
    if rc != 0:
        warnings.append(f"官方 PyPI 不通，安装阶段将尝试镜像源: {err[:200]}")
        updates["network_ok"] = False
    else:
        updates["network_ok"] = True

    # 6. 初始化 Installer 状态
    updates["retry_count"] = 0
    updates["max_retries"] = 3
    updates["current_index_url"] = "https://pypi.org/simple"
    updates["install_method"] = "uv"

    ok = len(issues) == 0
    return {
        "precheck_ok": ok,
        "issues": issues,
        "warnings": warnings,
        "updates": updates
    }


def _exec(cmd: list, timeout: int = 10) -> tuple:
    """
    执行命令，返回 (rc, stdout, stderr)。
    调用方需自行在 cmd 里拼好完整的 docker exec 前缀。
    """
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            encoding='utf-8',
            errors='replace'
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)