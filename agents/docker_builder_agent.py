"""
Docker Builder 子图 —— 环境准备阶段
职责：根据 Scanner 元数据生成 Dockerfile，构建镜像，启动容器。
绝不安装 Python 包，只准备系统环境。
"""

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langgraph.graph import StateGraph, END

from pipeline_state import PipelineState

# ---------- 状态定义 ----------
class DockerBuilderState(PipelineState, total=False):
    dockerfile_content: Optional[str]
    build_log: Optional[str]
    docker_image: Optional[str]
    container_id: Optional[str]
    docker_status: Optional[str]
    build_phase: Optional[str]
    container_name: Optional[str]
    docker_error: Optional[str]
    retry_count: Optional[int]
    fix_strategy: Optional[str]
    error: Optional[str]
    verify_ok: Optional[bool]
    precheck_ok: Optional[bool]


# ---------- 纯函数 ----------
def select_base_image(framework: str, python_version: str, pytorch_cuda_version: str = "12.1",
                      needs_project_build: bool = False, heavy_deps: list = None,
                      requirements_path: str = "") -> str:
    # 第一类：纯 CPU / 非 CUDA 框架
    if framework not in ("pytorch", "tensorflow", "jax"):
        py = python_version or "3.10"
        return f"python:{py}-slim"
    
    # 第二、三类：CUDA 框架
    cuda_ver = pytorch_cuda_version or "12.1"
    
    # 版本降级逻辑（保留）
    if requirements_path and os.path.exists(requirements_path):
        try:
            with open(requirements_path, 'r', encoding='utf-8') as f:
                req_content = f.read().lower()
            if re.search(r'torch\s*[=<>]+\s*1\.', req_content):
                cuda_ver = "11.8"
            if 'torch==2.0' in req_content and 'cu118' in req_content:
                cuda_ver = "11.8"
        except Exception:
            pass
    
    if heavy_deps:
        for h in heavy_deps:
            if h.get("meta", {}).get("max_cuda") == "11.8":
                cuda_ver = "11.8"
    
    # 第三类需要源码编译 → devel；第二类 PyPI wheel 即可 → runtime
    image_type = "devel" if needs_project_build else "runtime"
    
    return f"nvidia/cuda:{cuda_ver}.0-{image_type}-ubuntu22.04"


def get_python_cmd(python_version: str) -> str:
    if not python_version or python_version == "3.10":
        return "python3"
    # 截断 patch 版本号，只保留 major.minor（如 3.9.16 → 3.9）
    parts = python_version.split(".")
    if len(parts) >= 2:
        return f"python{parts[0]}.{parts[1]}"
    return f"python{python_version}"


def generate_dockerfile_skeleton(base_image: str, python_version: str, system_deps_formatted: str, heavy_hints: str = "") -> str:
    if not system_deps_formatted or not system_deps_formatted.strip():
        system_deps_formatted = "curl libgl1 libglib2.0-0"
    else:
        system_deps_formatted += " libgl1 libglib2.0-0"
    
    if python_version and python_version != "3.10":
        python_cmd = get_python_cmd(python_version)
        return f"""FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive

# 1. 启用 universe 源 + 系统依赖
RUN apt-get update && \\
    apt-get install -y software-properties-common && \\
    add-apt-repository -y universe && \\
    apt-get update && \\
    apt-get install -y {system_deps_formatted} && \\
    rm -rf /var/lib/apt/lists/*
RUN add-apt-repository -y ppa:deadsnakes/ppa && \\
    apt-get update && \\
    apt-get install -y {python_cmd} {python_cmd}-venv && \\
    rm -rf /var/lib/apt/lists/*
RUN {python_cmd} -m ensurepip --upgrade

# 2. 解除 PEP 668 限制，安装 uv，同时链 python 和 python3
RUN rm -f /usr/lib/python3.*/EXTERNALLY-MANAGED && \\
    {python_cmd} -m pip install --no-cache-dir uv
RUN ln -sf /usr/bin/{python_cmd} /usr/bin/python && \\
    ln -sf /usr/bin/{python_cmd} /usr/bin/python3

WORKDIR /workspace
COPY requirements_agent.txt /workspace/

# === Scanner 2.1: 以下包被标记为 heavy，未自动安装，需手动处理 ===
{heavy_hints}
CMD ["tail", "-f", "/dev/null"]
"""
    else:
        return f"""FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive

# 1. 启用 universe 源 + 系统依赖 + Python
RUN apt-get update && \\
    apt-get install -y software-properties-common && \\
    add-apt-repository -y universe && \\
    apt-get update && \\
    apt-get install -y python3 python3-pip {system_deps_formatted} && \\
    rm -rf /var/lib/apt/lists/*

# 2. 解除 PEP 668 限制，安装 uv
RUN rm -f /usr/lib/python3.*/EXTERNALLY-MANAGED && \\
    python3 -m pip install --no-cache-dir uv
RUN ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /workspace
COPY requirements_agent.txt /workspace/

# === Scanner 2.1: 以下包被标记为 heavy，未自动安装，需手动处理 ===
{heavy_hints}
CMD ["tail", "-f", "/dev/null"]
"""


# ---------- LLM 实例 ----------
_llm = None

def _get_llm():
    global _llm
    if _llm is None:
        _llm = ChatOpenAI(
            model="Pro/moonshotai/Kimi-K2.6",
            openai_api_key=(os.getenv("SILICONFLOW_API_KEY") or os.getenv("OPENAI_API_KEY") or ""),
            openai_api_base="https://api.siliconflow.cn/v1",
            temperature=0
        )
    return _llm


# ==========================================
# 节点 0：预检
# ==========================================
def precheck_node(state: DockerBuilderState) -> dict:
    project_path = state.get("project_path_win", "").strip()
    timestamps = state.get("timestamps", {})
    timestamps["docker_builder_start"] = time.time()
    framework = state.get("framework", "")
    
    if not project_path or not os.path.exists(project_path):
        return {
            "precheck_ok": False,
            "error": f"项目路径无效: {project_path}",
            "build_phase": "error"
        }
    
    if not framework:
        print(f"[DEBUG] framework 为空，按纯 CPU 项目处理")
        framework = "cpu"
    
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=10,
            check=False
        )
        if result.returncode != 0:
            return {
                "precheck_ok": False,
                "error": "Docker Desktop 未运行或无法连接。请启动 Docker Desktop 后重试。",
                "build_phase": "error"
            }
    except Exception as e:
        return {
            "precheck_ok": False,
            "error": f"Docker 检查失败: {e}",
            "build_phase": "error"
        }
    
    req_path = os.path.join(project_path, "requirements_agent.txt")
    if not os.path.exists(req_path):
        return {
            "precheck_ok": False,
            "error": f"缺少 requirements_agent.txt，请先运行 Scanner: {req_path}",
            "build_phase": "error"
        }
    
    return {
        "precheck_ok": True,
        "build_phase": "generating",
        "retry_count": 0,
        "error": "",
        "verify_ok": False,
        "timestamps": timestamps,
    }


# ==========================================
# 节点 1：LLM 决策（生成 Dockerfile）
# ==========================================
def call_model(state: DockerBuilderState) -> dict:
    print(f"\n🧠 [Docker Builder] 思考下一步... phase={state.get('build_phase')}")
    
    phase = state.get("build_phase", "generating")
    project_path = state.get("project_path_win", "")
    
    if phase == "generating":
        framework = state.get("framework", "unknown")
        python_version = state.get("python_version", "3.10")
        system_deps = state.get("system_deps", [])
        heavy_deps = state.get("heavy_deps", [])
        
        # === Scanner 2.1: 系统依赖（不再合并 heavy 包的 apt 依赖，由用户手动处理）===
        system_deps = sorted(set(system_deps))
        
        pytorch_cuda_version = state.get("pytorch_cuda_version", "12.1")
        needs_project_build = state.get("needs_project_build", False)
        req_path = os.path.join(project_path, "requirements_agent.txt")
        base_image = select_base_image(framework, python_version, pytorch_cuda_version, needs_project_build, heavy_deps, req_path)
        print(f"   选定基础镜像: {base_image}")
        
        system_deps_str = "\n".join(system_deps) if system_deps else ""
        
        system_prompt = f"""你是一位 Dockerfile 专家。请将以下系统依赖格式化为 apt-get 安装列表。

【输入】
system_deps: {system_deps_str}
基础镜像: {base_image}（基于 Ubuntu 22.04）
Python版本: {python_version}

【任务】
1. 将 system_deps 中的包名整理为 apt-get 可安装的包名。
2. Ubuntu 22.04 上 libgl1-mesa-glx 已拆分为 libgl1 和 libglx-mesa0，请替换。
3. 如果某个包在 Ubuntu 22.04 上不存在，去掉它或找替代。
4. 严禁添加任何 pip install / conda install / uv pip install 指令。

【输出格式】
只输出包名列表，每行一个，不要加反斜杠、不要加 markdown、不要加解释：
cmake
libgl1
libglx-mesa0

【约束】
- 如果 system_deps 为空，输出 "curl"（占位）。"""

        dialogs = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"为项目 {project_path} 格式化系统依赖")
        ]
        
        llm = _get_llm()
        response = llm.invoke(dialogs)
        print(f"🔧 [Docker Builder] system_deps 格式化完成")
        
        # ========== 修复：去掉 LLM 可能误加的符号，代码统一控制格式 ==========
        raw_deps = response.content.strip() if response.content else "curl"
        if "```" in raw_deps:
            raw_deps = re.sub(r"```[\w]*\n?", "", raw_deps).replace("```", "").strip()
        
        # 清洗：去掉行尾反斜杠、markdown 列表符号、多余空格
        lines = []
        for line in raw_deps.split("\n"):
            line = line.strip()
            line = line.rstrip("\\").strip()      # 去掉 LLM 可能加的续行符
            line = line.lstrip("-").strip()       # 去掉 markdown 列表符号
            if line and not line.startswith("```"):
                lines.append(line)
        
        # 统一格式化：代码控制续行符，不依赖 LLM
        formatted_lines = []
        for i, line in enumerate(lines):
            if i < len(lines) - 1:
                formatted_lines.append(line + " \\")
            else:
                formatted_lines.append(line)
        formatted_deps = "\n".join(formatted_lines)
        # ========== 修复结束 ==========
        
        # === Scanner 2.1: 生成 heavy 包提示注释 ===
        heavy_hints = ""
        if heavy_deps:
            hint_lines = []
            for h in heavy_deps:
                meta = h.get("meta", {})
                hint_lines.append(f"#   {h['name']}: {meta.get('reason', '')}")
                if meta.get("fix_hint"):
                    hint_lines.append(f"#      提示: {meta['fix_hint']}")
            heavy_hints = "\n".join(hint_lines)
        
        # 生成完整 Dockerfile
        dockerfile = generate_dockerfile_skeleton(base_image, python_version, formatted_deps, heavy_hints)
        
        return {
            "messages": [response],
            "dockerfile_content": dockerfile,
            "docker_image": base_image,
            "build_phase": "verifying",
        }
    
    elif phase == "fixing":
        build_log = state.get("build_log", "")
        dockerfile_content = state.get("dockerfile_content", "")
        
        system_prompt = f"""你是一位 Dockerfile 修复专家。请分析构建日志并修改 Dockerfile。

【当前 Dockerfile】
{dockerfile_content}

【构建日志（最后 1500 字符）】
{build_log[-1500:]}

【任务】
1. 识别错误类型（apt 包不存在 / CUDA 问题 / 网络超时 / 其他）。
2. 如果是 apt 包不存在，去掉该包或替换为 Ubuntu 22.04 等价包。
3. 输出完整的修改后 Dockerfile 文本（不要 markdown 包裹）。
4. 严禁添加任何 pip install / conda install / uv pip install 指令。"""

        dialogs = [
            SystemMessage(content=system_prompt),
            HumanMessage(content="修复 Dockerfile 构建错误")
        ]
        
        llm = _get_llm()
        response = llm.invoke(dialogs)
        
        new_content = response.content.strip() if response.content else dockerfile_content
        if "```" in new_content:
            new_content = re.sub(r"```[\w]*\n?", "", new_content).replace("```", "").strip()
        
        return {
            "messages": [response],
            "dockerfile_content": new_content,
            "build_phase": "verifying",
        }
    
    else:
        return {"messages": [AIMessage(content="阶段异常")]}


# ==========================================
# 节点 2：硬编码验证（Checkpoint）
# ==========================================
def verify_build(state: DockerBuilderState) -> dict:
    project_path = state.get("project_path_win", "")
    dockerfile_content = state.get("dockerfile_content", "")
    base_image = state.get("docker_image", "")
    
    # 1. Dockerfile 语法检查
    ok, msg = _check_dockerfile(dockerfile_content)
    if not ok:
        return {
            "verify_ok": False,
            "error": f"Dockerfile 检查失败: {msg}",
            "build_phase": "fixing",
        }
    
    # 2. 写入 Dockerfile
    dockerfile_path = os.path.join(project_path, "Dockerfile")
    try:
        with open(dockerfile_path, "w", encoding="utf-8") as f:
            f.write(dockerfile_content)
    except Exception as e:
        return {
            "verify_ok": False,
            "error": f"写入 Dockerfile 失败: {e}",
            "build_phase": "fixing",
        }
    
    # 3. 推断容器名和镜像名
    repo_name = Path(project_path).name.lower()
    image_name = f"{repo_name}:latest"
    container_name = f"{repo_name}_container"
    
    # === 新增：幂等检查，镜像已存在则跳过构建 ===
    check = subprocess.run(
        ["docker", "images", "-q", image_name],
        capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=10
    )
    build_log = ""
    if check.stdout.strip():
        print(f"✅ [Docker Builder] 镜像 {image_name} 已存在，跳过构建")
        build_log = "镜像已存在，跳过构建"
    else:
        # 4. docker build
        print(f"🔨 [Docker Builder] 构建镜像: {image_name}")
        try:
            result = subprocess.run(
                ["docker", "build", "-t", image_name, project_path],
                capture_output=True, text=True, encoding="utf-8", errors="ignore",
                timeout=1800, check=False
            )
            build_log = result.stderr + result.stdout
        except subprocess.TimeoutExpired:
            return {
                "verify_ok": False, "error": "docker build 超时（600秒）",
                "build_log": "超时", "build_phase": "fixing",
            }
        except Exception as e:
            return {
                "verify_ok": False, "error": f"docker build 异常: {e}",
                "build_phase": "fixing",
            }
        
        if result.returncode != 0:
            print(f"\n❌ [Docker Builder] docker build 失败 (rc={result.returncode})")
            print(f"❌ [Docker Builder] 日志最后 2000 字符:\n{build_log[-2000:]}")
            return {
                "verify_ok": False, "error": f"docker build 失败: {build_log}",
                "build_log": build_log, "docker_image": image_name,
                "build_phase": "fixing",
            }
        
        print(f"✅ [Docker Builder] 镜像构建成功: {image_name}")
    
    # 5. 幂等启动容器
    print(f"🚀 [Docker Builder] 启动容器: {container_name}")
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=10,
            check=False
        )
        
        run_result = subprocess.run(
            [
                "docker", "run", "-d",
                "--name", container_name,
                "-v", f"{project_path}:/workspace",
                image_name,
                "tail", "-f", "/dev/null"
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=30,
            check=False
        )
        
        if run_result.returncode != 0:
            return {
                "verify_ok": False,
                "error": f"docker run 失败: {run_result.stderr[:500]}",
                "build_log": build_log,
                "docker_image": image_name,
                "build_phase": "fixing",
            }
        
        container_id = run_result.stdout.strip()
        
        time.sleep(1)
        check = subprocess.run(
            ["docker", "ps", "-q", "-f", f"name={container_name}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=10
        )
        if not check.stdout.strip():
            return {
                "verify_ok": False,
                "error": "容器启动后立刻退出，请检查 Dockerfile 的 CMD/ENTRYPOINT",
                "build_log": build_log,
                "docker_image": image_name,
                "build_phase": "fixing",
            }
        
    except Exception as e:
        return {
            "verify_ok": False,
            "error": f"docker run 异常: {e}",
            "build_phase": "fixing",
        }
    
    print(f"✅ [Docker Builder] 容器启动成功: {container_id[:12]}")
    
    return {
        "verify_ok": True,
        "dockerfile_path": dockerfile_path,
        "docker_image": image_name,
        "container_id": container_id,
        "container_name": container_name,
        "build_log": build_log,
        "docker_status": "success",
        "build_phase": "built",
        "error": "",
    }


def _check_dockerfile(content: str) -> tuple[bool, str]:
    # 提取非注释、非空行（# 开头的全是注释）
    code_lines = []
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            code_lines.append(stripped)
    code = "\n".join(code_lines)
    
    required = ["FROM", "WORKDIR", "COPY requirements_agent.txt", "tail", "-f", "/dev/null"]
    for r in required:
        if r not in code:
            return False, f"缺少必要指令: {r}"
    
    forbidden = ["pip install torch", "pip install tensorflow", "conda install", "uv pip install"]
    for f in forbidden:
        if f in code.lower():
            return False, f"越界: 包含 '{f}'"
    
    return True, "ok"


# ==========================================
# 节点 3：修复分支
# ==========================================
def fix_node(state: DockerBuilderState) -> dict:
    retries = state.get("retry_count", 0)
    error = state.get("error", "").lower()
    build_log = state.get("build_log", "").lower()
    # === 新增 ===
    print(f"\n🔧 [Docker Builder fix_node] retry={retries}")
    print(f"🔧 [Docker Builder fix_node] error={error[:500]!r}")
    print(f"🔧 [Docker Builder fix_node] build_log={build_log[:500]!r}")
    # ===
    
    if retries >= 3:
        return {
            "retry_count": retries,
            "fix_strategy": "abort",
            "error": f"Docker 构建失败，已达最大重试次数。{state.get('error', '')}"
        }
    
    strategy = "retry"
    
    if "docker desktop" in error or "daemon" in error or "cannot connect" in error:
        strategy = "abort"
    elif "cuda" in error or "nvidia" in error or "driver" in error:
        strategy = "abort"
    elif "unable to locate package" in build_log or "no package" in build_log:
        strategy = "retry"
    elif "timeout" in error or "tls handshake" in error:
        strategy = "retry"
    else:
        strategy = "retry"
    
    return {
        "retry_count": retries + 1,
        "fix_strategy": strategy,
        "build_phase": "fixing" if strategy == "retry" else "error",
        "error": state.get("error", ""),
    }


# ==========================================
# 验证后路由
# ==========================================
def verify_router(state: DockerBuilderState) -> str:
    if not state.get("verify_ok"):
        return "fix"
    phase = state.get("build_phase")
    if phase == "built":
        return "extract"
    return "agent"


# ==========================================
# 节点 4：提取结果（出口）
# ==========================================
def extract_result(state: DockerBuilderState) -> dict:
    dockerfile_path = state.get("dockerfile_path", "")
    image_name = state.get("docker_image", "")
    container_id = state.get("container_id", "")
    container_name = state.get("container_name", "")
    
    summary = AIMessage(
        content=(
            f"✅ Docker 环境准备完成。\n"
            f"镜像: {image_name}\n"
            f"容器: {container_name} ({container_id[:12] if container_id else 'N/A'})\n"
            f"Dockerfile: {dockerfile_path}"
        )
    )

    timestamps = state.get("timestamps", {})
    timestamps["docker_builder_end"] = time.time()
    elapsed = timestamps["docker_builder_end"] - timestamps["docker_builder_start"]
    print(f"\n⏱️ [Docker Builder] 阶段耗时: {elapsed:.1f}s")
    
    return {
        "dockerfile_path": dockerfile_path,
        "docker_image": image_name,
        "container_id": container_id,
        "container_name": container_name,
        "docker_status": "success",
        "messages": [summary],
        "next_agent": "installer",
        "current_agent": "docker_builder",
        "build_phase": "built",
        "timestamps": timestamps,
    }


# ==========================================
# 构建子图
# ==========================================
def create_docker_builder_agent():
    workflow = StateGraph(DockerBuilderState)

    workflow.add_node("precheck", precheck_node)
    workflow.add_node("agent", call_model)
    workflow.add_node("verify", verify_build)
    workflow.add_node("fix", fix_node)
    workflow.add_node("extract", extract_result)

    workflow.set_entry_point("precheck")

    workflow.add_conditional_edges(
        "precheck",
        lambda s: "agent" if s.get("precheck_ok") else "fix",
        {"agent": "agent", "fix": "fix"}
    )

    workflow.add_edge("agent", "verify")

    workflow.add_conditional_edges(
        "verify",
        verify_router,
        {"agent": "agent", "extract": "extract", "fix": "fix"}
    )

    workflow.add_conditional_edges(
        "fix",
        lambda s: "agent" if s.get("fix_strategy") == "retry" else END,
        {"agent": "agent", END: END}
    )

    workflow.add_edge("extract", END)

    return workflow.compile()


docker_builder_agent_node = create_docker_builder_agent()