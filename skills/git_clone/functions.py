"""
Git Clone Skill 函数（Windows 版）
使用 subprocess 直接执行，不依赖 Shell 语法，兼容 CMD / PowerShell / Bash。
"""

import json
import os
import shutil
import subprocess
import time          # ← 新增（重命名兜底用）
from langchain_core.tools import tool


# ---------- 加速地址池 ----------
PROXY_URLS = [
    ("gh-proxy.org", lambda url: f"https://gh-proxy.org/{url}", 30),
    ("gitdelivr.net", lambda url: f"https://gitdelivr.net/{url.replace('https://github.com/', 'github.com/')}", 30),
    ("direct", lambda url: url, 60),
]


def _run_git_clone(clone_url: str, target_root: str, proxy_url: str = "", timeout: int = 30) -> dict:
    """
    使用 subprocess.run 直接执行 git clone，不拼接 shell 命令字符串。
    通过 cwd 和 env 参数控制工作目录和环境变量。
    """
    # 确保目标目录存在
    os.makedirs(target_root, exist_ok=True)

    # 构建环境变量
    env = os.environ.copy()
    if proxy_url:
        env["HTTP_PROXY"] = proxy_url
        env["HTTPS_PROXY"] = proxy_url

    # 提取仓库名作为目标文件夹
    repo_name = clone_url.rstrip("/").split("/")[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]

    target_path = os.path.join(target_root, repo_name)

    # ← 修改：幂等克隆。Windows 下 shutil.rmtree 对占用/只读目录不可靠，必须系统命令兜底。
    if os.path.exists(target_path):
        # 第一层：标准删除
        shutil.rmtree(target_path, ignore_errors=True)
        
        # 第二层：Windows 强制删除（处理只读、句柄残留、权限问题）
        if os.path.exists(target_path):
            subprocess.run(
                ["cmd", "/c", "rmdir", "/s", "/q", target_path],
                capture_output=True,
                check=False,
                timeout=10
            )
        
        # 第三层：如果还被占用（如 IDE 锁定），重命名占位，保证 git clone 能继续
        if os.path.exists(target_path):
            try:
                os.rename(target_path, target_path + "_old_" + str(int(time.time())))
            except Exception:
                # 实在清不掉，返回明确错误，让 fix_node 处理
                return {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": f"目标路径已存在且无法清理: {target_path}"
                }

    cmd = ["git", "clone", clone_url, repo_name]

    try:
        result = subprocess.run(
            cmd,
            cwd=target_root,      # 直接指定工作目录，不用 cd
            env=env,              # 直接指定环境变量，不用 set
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace"
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr
        }
    except subprocess.TimeoutExpired as e:
        return {
            "returncode": 1,
            "stdout": e.stdout.decode("utf-8", errors="replace") if e.stdout else "",
            "stderr": f"命令执行超时（{timeout}秒）"
        }
    except Exception as e:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": f"subprocess 异常: {type(e).__name__}: {e}"
        }


@tool
def skill_git_clone(repo_url: str, target_root: str, proxy_url: str = "") -> str:
    """
    将 Git 仓库克隆到本地 Windows 目录。
    自动处理 GitHub 加速、多源重试、代理配置、目录冲突检查。

    参数:
        repo_url: Git 仓库完整 URL (如 https://github.com/user/repo.git)
        target_root: 目标根目录 (Windows 路径, 如 D:\\Projects)
        proxy_url: 可选，HTTP/HTTPS 代理地址 (如 http://127.0.0.1:7890)
    返回:
        JSON 字符串，包含 success/local_path/used_proxy/error 等
    """
    try:
        # 强制使用绝对路径，避免 "." 漂移
        target_root = os.path.abspath(target_root)
        
        repo_name = repo_url.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        target_path = os.path.join(target_root, repo_name)

        # 多源重试克隆
        for proxy_name, url_template, timeout in PROXY_URLS:
            # 非 GitHub 仓库只走直连
            if proxy_name != "direct" and not (
                repo_url.startswith("https://github.com") or
                repo_url.startswith("http://github.com")
            ):
                continue

            clone_url = url_template(repo_url)
            print(f"DEBUG 尝试 [{proxy_name}]: {clone_url}")

            result = _run_git_clone(clone_url, target_root, proxy_url, timeout)

            print(f"DEBUG [{proxy_name}] returncode={result['returncode']}")
            if result["stderr"]:
                print(f"DEBUG [{proxy_name}] stderr={result['stderr'][:300]}")

            if result["returncode"] == 0:
                return json.dumps({
                    "success": True,
                    "local_path": target_path,
                    "used_proxy": proxy_name,
                    "repo_name": repo_name,
                })
            # 失败则继续下一个源

        # 5. 全部失败
        return json.dumps({
            "success": False,
            "error": "所有加速源及直连均失败。请检查网络或代理。",
            "repo_url": repo_url,
        })

    except Exception as e:
        return json.dumps({
            "success": False,
            "error": f"skill_git_clone 内部异常: {type(e).__name__}: {e}",
            "repo_url": repo_url if 'repo_url' in dir() else "未知"
        })


# ---------- 新增：仓库完整性检查 ----------
def check_repo_integrity(repo_path: str, timeout: int = 5) -> tuple[bool, str]:
    """
    最小必要完整性检查。任何失败 -> 调用方应直接重克隆。
    返回: (是否通过, 原因)
    """
    git_dir = os.path.join(repo_path, ".git")
    
    # 1. .git 必须是目录，且 HEAD 存在
    if not os.path.isdir(git_dir):
        return False, ".git is not a directory"
    
    if not os.path.isfile(os.path.join(git_dir, "HEAD")):
        return False, ".git/HEAD missing"
    
    # 2. Git 元数据本地可读（纯本地，5秒超时防死锁）
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False
        )
    except subprocess.TimeoutExpired:
        return False, "git command timeout (repo likely corrupted)"
    except Exception as e:
        return False, f"git command exception: {e}"
    
    if result.returncode != 0:
        stderr = result.stderr.strip()[:200] if result.stderr else "unknown"
        return False, f"git corrupt: {stderr}"
    
    head = result.stdout.strip()
    if len(head) != 40 or not all(c in "0123456789abcdef" for c in head.lower()):
        return False, f"invalid HEAD reference"
    
    # 3. 工作区有实质内容（防克隆中断 checkout 阶段）
    try:
        entries = os.listdir(repo_path)
    except Exception as e:
        return False, f"cannot list workspace: {e}"
    
    has_content = any(name != ".git" for name in entries)
    if not has_content:
        return False, "workspace empty (clone likely interrupted)"
    
    return True, "ok"


TOOLS = [skill_git_clone]