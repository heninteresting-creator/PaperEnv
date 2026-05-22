"""
Installer Skill 函数 —— 纯 uv 路线
职责：原子安装（单层，不内部换源）、验证导入、日志解析失败包。
注意：换源重试由 agent 状态机控制，本 skill 只执行一次。
"""

import json
import re
import subprocess
import traceback
from typing import List

from langchain_core.tools import tool


# ============================================================
# 辅助函数
# ============================================================
def _exec_in_container(container_name: str, cmd: list, timeout: int = 300) -> tuple:
    """在容器内执行命令，返回 (returncode, stdout, stderr)"""
    full_cmd = ["docker", "exec", container_name] + cmd
    print(f"[INSTALLER DEBUG] 执行: {' '.join(full_cmd)!r}")
    
    try:
        result = subprocess.run(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            encoding='utf-8',
            errors='replace'
        )
        print(f"[INSTALLER DEBUG] rc={result.returncode}")
        if result.stderr:
            print(f"[INSTALLER DEBUG] stderr 前500: {result.stderr[:500]!r}")
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        print(f"[INSTALLER DEBUG] ❌ 超时（{timeout}秒）")
        return -1, "", f"命令超时（{timeout}秒）"
    except Exception as e:
        print(f"[INSTALLER DEBUG] ❌ 异常: {e}")
        return -1, "", str(e)


def _parse_failed_packages_from_log(stderr: str) -> List[str]:
    """
    从 uv/pip 的 stderr 中提取安装失败的包名。
    覆盖常见错误模式：
      - Could not find a version that satisfies the requirement xxx
      - No matching distribution found for xxx
      - Failed to download xxx
      - error: failed to build xxx
    """
    failed = set()
    
    patterns = [
        r'Could not find a version that satisfies the requirement\s+([a-zA-Z0-9_.-]+)',
        r'No matching distribution found for\s+([a-zA-Z0-9_.-]+)',
        r'Failed to download\s+([a-zA-Z0-9_.-]+)',
        r'Failed to build\s+([a-zA-Z0-9_.-]+)',
        r'error:\s*failed to install\s+([a-zA-Z0-9_.-]+)',
        r'No package\s+([a-zA-Z0-9_.-]+)\s+found',
    ]
    
    for pat in patterns:
        for match in re.finditer(pat, stderr, re.IGNORECASE):
            pkg = match.group(1).strip()
            if pkg:
                failed.add(pkg)
    
    return list(failed)


# ============================================================
# Skill 工具
# ============================================================
@tool
def skill_install_dependencies(
    container_name: str,
    requirements_path: str = "/app/requirements_agent.txt",
    extra_index_url: str = "",
    index_url: str = "https://pypi.org/simple",
    timeout: int = 600
) -> str:
    """
    单层 uv 安装。不内部换源，不内部重试。
    失败时返回完整 stderr 和解析出的 failed_packages，供上层剔包重试。
    """
    print(f"\n[INSTALLER DEBUG] === skill_install_dependencies 开始 ===")
    print(f"[INSTALLER DEBUG] container={container_name}")
    print(f"[INSTALLER DEBUG] requirements={requirements_path}")
    print(f"[INSTALLER DEBUG] extra_index_url={extra_index_url!r}")
    print(f"[INSTALLER DEBUG] index_url={index_url!r}")
    
    # 1. 检查容器内 requirements 存在
    rc, _, _ = _exec_in_container(container_name, ["test", "-f", requirements_path], timeout=10)
    if rc != 0:
        return json.dumps({
            "success": False,
            "error": f"容器内未找到 requirements: {requirements_path}",
            "failed_packages": []
        })
    
    # 2. 构建 uv 命令
    cmd = ["uv", "pip", "install", "-r", requirements_path]
    
    if extra_index_url:
        cmd.extend(["--extra-index-url", extra_index_url])
    
    # 只有非默认且非空才传 --index-url，避免覆盖 uv 默认行为
    if index_url and index_url != "https://pypi.org/simple":
        cmd.extend(["--index-url", index_url])
    
    # 3. 执行单层安装
    rc, out, err = _exec_in_container(container_name, cmd, timeout=timeout)
    
    if rc == 0:
        print(f"[INSTALLER DEBUG] 安装成功")
        return json.dumps({
            "success": True,
            "log": out[:1000],
            "index_url": index_url,
            "extra_index_url": extra_index_url,
            "failed_packages": []
        })
    
    # 4. 失败：解析失败包，返回日志
    failed = _parse_failed_packages_from_log(err)
    print(f"[INSTALLER DEBUG] 安装失败，解析到失败包: {failed}")
    
    return json.dumps({
        "success": False,
        "failed_packages": failed,
        "log": err[:2000],
        "index_url": index_url,
        "extra_index_url": extra_index_url,
        "error": f"uv pip install 失败 (rc={rc}): {err[:500]}"
    })


@tool
def skill_verify_installation(
    container_name: str,
    package_list: List[str]
) -> str:
    """
    验证容器内指定包是否能 import。
    模块名映射 + 双层版本获取（__version__ → importlib.metadata）。
    """
    print(f"\n[INSTALLER DEBUG] === skill_verify_installation 开始 ===")
    print(f"[INSTALLER DEBUG] container={container_name}")
    print(f"[INSTALLER DEBUG] packages={package_list[:30]}...")
    
    failed = []
    details = {}
    
    # 模块名映射表（pip 包名 → Python 模块名）
    module_map = {
        "opencv-python-headless": "cv2",
        "opencv-python": "cv2",
        "pillow": "PIL",
        "scikit-learn": "sklearn",
        "scikit-image": "skimage",
        "pyyaml": "yaml",
        "python-dateutil": "dateutil",
        "attrs": "attrs",
    }
    
    for pkg in package_list:
        # 提取纯净包名（去掉版本号）
        clean_pkg = pkg.split('==')[0].split('>=')[0].split('<=')[0].strip()
        
        # 查映射表，没有则取第一个词
        module_name = module_map.get(
            clean_pkg.lower(),
            clean_pkg.split('-')[0].split('_')[0]
        )
        
        print(f"[INSTALLER DEBUG] 验证 {clean_pkg} → import {module_name}")
        
        # 双层版本获取脚本
        script = (
            f"import {module_name}\n"
            f"try:\n"
            f"    print({module_name}.__version__)\n"
            f"except AttributeError:\n"
            f"    import importlib.metadata\n"
            f"    print(importlib.metadata.version('{clean_pkg}'))"
        )
        
        rc, out, err = _exec_in_container(
            container_name,
            ["python", "-c", script],
            timeout=30
        )
        
        if rc == 0:
            version = out.strip()
            details[clean_pkg] = {"ok": True, "version": version}
            print(f"[INSTALLER DEBUG]   ✓ {version}")
        else:
            failed.append(clean_pkg)
            details[clean_pkg] = {"ok": False, "error": err[:200]}
            print(f"[INSTALLER DEBUG]   ✗ {err[:200]!r}")
    
    success = len(failed) == 0
    return json.dumps({
        "success": success,
        "failed_packages": failed,
        "details": details
    })


# 导出工具列表
TOOLS = [skill_install_dependencies, skill_verify_installation]