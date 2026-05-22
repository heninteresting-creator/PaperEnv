"""
Docker Builder Skill 函数
职责：生成极简 Dockerfile（只含基础环境），构建镜像，启动容器并注入代理。
注意：不在构建阶段安装任何项目依赖，依赖安装由 Installer Agent 在运行容器中执行。
"""

import json
import os
import re
import subprocess
import traceback
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool


# ============================================================
# 1. 基础镜像推断
# ============================================================

def _infer_base_image(req_path: str) -> str:
    """根据 requirements 推断基础镜像"""
    try:
        with open(req_path, 'r', encoding='utf-8') as f:
            content = f.read().lower()
    except Exception:
        return "python:3.11-slim"

    # === 关键修复：有 torch/tensorflow 就用 CUDA 镜像（裸包名也适用）===
    if any(k in content for k in ['torch', 'tensorflow']):
        return "nvidia/cuda:13.0.0-runtime-ubuntu22.04"

    # 兼容旧格式：如果有明确的 CUDA 版本标记（如 cu130）
    cuda_match = re.search(r'cu(\d{2,3})', content)
    if cuda_match:
        cuda_ver = cuda_match.group(1)
        major = cuda_ver[0]
        minor = cuda_ver[1:] if len(cuda_ver) > 1 else "0"
        return f"nvidia/cuda:{major}.{minor}.0-runtime-ubuntu22.04"

    # 有 conda/mamba 需求
    if any(k in content for k in ['mamba', 'conda']):
        return "condaforge/miniforge:latest"

    # 默认 CPU
    return "python:3.11-slim"


# ============================================================
# 2. Dockerfile 生成（极简版，不安装依赖）
# ============================================================
def _generate_dockerfile(project_path: str, req_path: str, force: bool = False) -> str:
    """生成极简 Dockerfile：只装基础环境（含编译工具），不装项目依赖"""
    print(f"\n[SKILL DEBUG] --- _generate_dockerfile 开始 ---")
    print(f"[SKILL DEBUG] project_path={project_path!r}")
    print(f"[SKILL DEBUG] req_path={req_path!r}")
    print(f"[SKILL DEBUG] force={force}")
    
    dockerfile_path = os.path.join(project_path, "Dockerfile")
    abs_path = os.path.abspath(dockerfile_path)
    print(f"[SKILL DEBUG] 目标路径: {dockerfile_path}")
    print(f"[SKILL DEBUG] 绝对路径: {abs_path}")
    print(f"[SKILL DEBUG] project_path 是否存在: {os.path.exists(project_path)}")
    print(f"[SKILL DEBUG] project_path 是目录: {os.path.isdir(project_path)}")
    print(f"[SKILL DEBUG] Dockerfile 已存在: {os.path.exists(dockerfile_path)}")
    
    # === 检查现有文件：强制覆盖 / 空文件 / 无 FROM → 删除重新生成 ===
    if os.path.exists(dockerfile_path):
        sz = os.path.getsize(dockerfile_path)
        print(f"[SKILL DEBUG] 文件已存在，大小={sz}B")
        
        should_regen = force or sz < 50
        if not should_regen:
            try:
                with open(dockerfile_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                if "FROM" not in content:
                    should_regen = True
                    print(f"[SKILL] Dockerfile 内容非法，强制删除: {dockerfile_path}")
            except Exception as e:
                should_regen = True
                print(f"[SKILL] 读取异常，强制删除: {e}")
        
        if should_regen:
            os.remove(dockerfile_path)
            print(f"[SKILL DEBUG] 已删除旧文件，准备重新生成")
        else:
            print(f"[SKILL DEBUG] Dockerfile 内容有效，直接返回")
            return dockerfile_path
    
    # 确保目录存在
    if not os.path.isdir(project_path):
        raise ValueError(f"project_path 不是目录: {project_path}")
    
    base_image = _infer_base_image(req_path)
    print(f"[SKILL] 使用基础镜像: {base_image}")
    
    # === 关键改动：去掉 libnccl2，预装 build-essential cmake ===
    content = (
        f"FROM {base_image}\n\n"
        f"WORKDIR /app\n\n"
        f"# 换国内 apt 源 + 安装构建工具 + Python3\n"
        f"RUN sed -i 's|//.*ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list && "
        f"sed -i 's|//.*ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/*.list 2>/dev/null || true && "
        f"apt-get update && "
        f"apt-get install -y --no-install-recommends "
        f"build-essential cmake g++ wget bzip2 ca-certificates git python3 python3-pip python3-dev && "
        f"rm -rf /var/lib/apt/lists/*\n\n"
        f"# 预装 uv（走国内 PyPI 源）\n"
        f"RUN python3 -m pip install uv -i https://pypi.tuna.tsinghua.edu.cn/simple  || "
        f"python3 -m pip install uv\n\n"
        f"RUN mkdir -p /app && chmod 777 /app\n\n"
        f'CMD ["tail", "-f", "/dev/null"]\n'
    )
    print(f"[SKILL DEBUG] Dockerfile 内容长度: {len(content)} 字节")
    print(f"[SKILL DEBUG] 准备写入...")
    
    try:
        with open(dockerfile_path, 'w', encoding='utf-8') as f:
            written = f.write(content)
        print(f"[SKILL DEBUG] write() 返回值: {written}")
    except Exception as e:
        print(f"[SKILL DEBUG] 写入异常: {type(e).__name__}: {e}")
        raise
    
    # 立即二次确认
    exists = os.path.exists(dockerfile_path)
    size = os.path.getsize(dockerfile_path) if exists else -1
    print(f"[SKILL DEBUG] 写入后 exists={exists}, size={size}")
    
    if not exists:
        raise RuntimeError(f"open().write() 后文件仍然不存在: {abs_path}")
    if size < 50:
        raise RuntimeError(f"写入后文件异常，仅 {size} 字节")
    
    print(f"[SKILL DEBUG] --- _generate_dockerfile 成功返回 ---")
    return dockerfile_path


# ============================================================
# 3. Skill 工具
# ============================================================
@tool
def skill_generate_dockerfile(project_path: str, requirements_path: str, force: bool = False) -> str:
    """检查/生成极简 Dockerfile（不安装依赖）。force=True 时强制覆盖旧文件。"""
    print(f"\n[SKILL DEBUG] === skill_generate_dockerfile 开始 ===")
    print(f"[SKILL DEBUG] project_path={project_path!r}")
    print(f"[SKILL DEBUG] requirements_path={requirements_path!r}")
    print(f"[SKILL DEBUG] force={force}")
    
    try:
        path = _generate_dockerfile(project_path, requirements_path, force=force)
        print(f"[SKILL DEBUG] === 返回 success, path={path!r} ===\n")
        return json.dumps({"success": True, "dockerfile_path": path})
    except Exception as e:
        print(f"[SKILL DEBUG] 顶层异常捕获: {type(e).__name__}: {e}")
        traceback.print_exc()
        return json.dumps({"success": False, "error": str(e)})


@tool
def skill_docker_build(project_path: str, image_name: str = None) -> str:
    """构建 Docker 镜像"""
    print(f"\n[SKILL DEBUG] === skill_docker_build 开始 ===")
    print(f"[SKILL DEBUG] project_path={project_path!r}")
    print(f"[SKILL DEBUG] image_name={image_name!r}")
    
    if image_name is None or not image_name.strip():
        project_name = os.path.basename(os.path.normpath(project_path)).lower()
        image_name = f"{project_name}-env:latest"
        print(f"[SKILL DEBUG] 自动推断镜像名: {image_name}")

    if not project_path:
        return json.dumps({"success": False, "error": "project_path 为空"})
    
    abs_project = os.path.abspath(project_path)
    print(f"[SKILL DEBUG] 绝对路径: {abs_project}")
    
    if not os.path.exists(project_path):
        return json.dumps({"success": False, "error": f"project_path 不存在: {project_path}"})
    
    files = os.listdir(project_path)
    print(f"[SKILL DEBUG] 目录内容: {files}")
    
    if "Dockerfile" not in files:
        return json.dumps({"success": False, "error": "缺少 Dockerfile"})
    
    df_size = os.path.getsize(os.path.join(project_path, "Dockerfile"))
    print(f"[SKILL DEBUG] Dockerfile 存在，大小: {df_size}B")
    
    try:
        # 关键：禁用 BuildKit
        env = os.environ.copy()
        env["DOCKER_BUILDKIT"] = "0"
        
        print(f"[SKILL DEBUG] 执行: DOCKER_BUILDKIT=0 docker build -t {image_name} {project_path}")
        
        # 根治：不用 capture_output=True/text=True，避免 _readerthread 编码崩溃
        result = subprocess.run(
            ["docker", "build", "-t", image_name, project_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600,
            env=env
        )
        
        # 手动安全 decode
        stdout = result.stdout.decode('utf-8', errors='replace') if result.stdout else ""
        stderr = result.stderr.decode('utf-8', errors='replace') if result.stderr else ""
        
        print(f"[SKILL DEBUG] docker build returncode: {result.returncode}")
        print(f"[SKILL DEBUG] stderr 前1000字: {stderr[:1000]!r}")
        
        if result.returncode != 0:
            return json.dumps({
                "success": False,
                "error": f"docker build 失败 (rc={result.returncode}): {stderr[:1000]}"
            })
        
        # 验证镜像存在
        inspect = subprocess.run(
            ["docker", "images", "--filter", f"reference={image_name}", "--format", "{{.Repository}}:{{.Tag}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env
        )
        inspect_out = inspect.stdout.decode('utf-8', errors='replace') if inspect.stdout else ""
        print(f"[SKILL DEBUG] docker images 输出: {inspect_out!r}")
        
        if image_name not in inspect_out:
            return json.dumps({
                "success": False,
                "error": f"镜像 {image_name} 未找到，可能标签未打上"
            })
        
        print(f"[SKILL DEBUG] === 返回 success ===\n")
        return json.dumps({"success": True, "image_name": image_name})
        
    except subprocess.TimeoutExpired:
        print(f"[SKILL DEBUG] ❌ 构建超时（120秒）")
        return json.dumps({"success": False, "error": "docker build 超时，可能 BuildKit 未禁用或网络卡死"})
    except Exception as e:
        traceback.print_exc()
        return json.dumps({"success": False, "error": str(e)})


@tool
def skill_docker_run(
    image_name: str = None,
    container_name: str = None,
    project_path: str = "",
    memory_limit: str = "14g",
    network_mode: str = "bridge",
    proxy_url: str = ""
) -> str:
    """
    启动容器，分配大内存，挂载项目源码。
    如有代理，自动替换 127.0.0.1 为 host.docker.internal 并注入环境变量。
    """
    print(f"\n[SKILL DEBUG] === skill_docker_run 开始 ===")
    print(f"[SKILL DEBUG] image_name={image_name!r}")
    print(f"[SKILL DEBUG] container_name={container_name!r}")
    print(f"[SKILL DEBUG] project_path={project_path!r}")

    if project_path:
        project_name = os.path.basename(os.path.normpath(project_path)).lower()
        if image_name is None or not image_name.strip():
            image_name = f"{project_name}-env:latest"
            print(f"[SKILL DEBUG] 自动推断镜像名: {image_name}")
        if container_name is None or not container_name.strip():
            container_name = f"{project_name}-container"
            print(f"[SKILL DEBUG] 自动推断容器名: {container_name}")
    
    print(f"[SKILL DEBUG] image_name={image_name!r}")
    print(f"[SKILL DEBUG] container_name={container_name!r}")
    
    if not project_path:
        return json.dumps({"success": False, "error": "project_path 为空"})
    
    # 清理同名旧容器（也加 encoding 防护）
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True, text=True,
        encoding='utf-8',
        errors='replace'
    )
    
    # 构建命令
    cmd = [
        "docker", "run", "-d",
        "-v", f"{project_path}:/app",
        "--memory", memory_limit,
        "--memory-swap", memory_limit,
        "--network", network_mode,
    ]
    
    # 注入代理环境变量（关键：127.0.0.1 → host.docker.internal）
    if proxy_url:
        container_proxy = proxy_url.replace("127.0.0.1", "host.docker.internal")
        cmd.extend([
            "-e", f"HTTP_PROXY={container_proxy}",
            "-e", f"HTTPS_PROXY={container_proxy}",
            "-e", f"http_proxy={container_proxy}",
            "-e", f"https_proxy={container_proxy}",
        ])
    
    cmd.extend(["--name", container_name, image_name])
    print(f"[SKILL DEBUG] 执行命令: {' '.join(cmd)!r}")
    
    try:
        # 关键修复：强制 UTF-8 编码
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=60,
            encoding='utf-8',      # ← 强制 UTF-8
            errors='replace'       # ← 解不了的字符用替换
        )
        
        print(f"[SKILL DEBUG] returncode={result.returncode}")
        print(f"[SKILL DEBUG] stdout={result.stdout!r}")
        print(f"[SKILL DEBUG] stderr={result.stderr!r}")
        
        if result.returncode == 0:
            container_id = result.stdout.strip()
            return json.dumps({
                "success": True,
                "container_id": container_id,
                "container_name": container_name,
                "memory_limit": memory_limit,
                "network_mode": network_mode,
                "proxy_injected": bool(proxy_url)
            })
        else:
            return json.dumps({"success": False, "error": result.stderr})
    except Exception as e:
        traceback.print_exc()
        return json.dumps({"success": False, "error": str(e)})


# 导出工具列表
TOOLS = [skill_generate_dockerfile, skill_docker_build, skill_docker_run]