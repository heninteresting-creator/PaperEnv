"""
Installer 子图 (Subgraph) —— 纯 uv 路线（LLM 增强版）
职责：在运行中的 Docker 容器内安装依赖，验证导入，支持换源/剔包重试。
设计：确定性规则引擎 + LLM 决策层（绑定 web_search + manage_file）
"""

import os
import json
import re
import time
import subprocess
import importlib
from collections import deque
from typing import Optional, List, Dict, Any

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from pipeline_state import PipelineState
from tools.file_tools import manage_file
from tools.web_search import web_search  # ← 路径确认：D:\code\PaperEnv\tools\web_search.py

# Installer LLM 工具集
INSTALLER_TOOLS = [manage_file, web_search]

# 注意：installer_tool_node 定义了但没在图里用（_llm_analyze_error 内联调用）
# 保留定义供后续扩展，当前 LLM 分析是内联闭环
installer_tool_node = ToolNode(INSTALLER_TOOLS)


def _get_installer_llm_with_tools():
    """返回绑定工具的 Installer LLM（体现工具绑定能力）"""
    llm = ChatOpenAI(
        model="Pro/moonshotai/Kimi-K2.6",
        openai_api_key=(os.getenv("SILICONFLOW_API_KEY") or os.getenv("OPENAI_API_KEY") or ""),
        openai_api_base="https://api.siliconflow.cn/v1",
        temperature=0,
    )
    return llm.bind_tools(INSTALLER_TOOLS)


# 导入底层预检
precheck_module = importlib.import_module("skills.installer.precheck")
run_precheck = precheck_module.run_precheck


# ============================================================
# 状态定义
# ============================================================
class InstallerState(PipelineState, total=False):
    # === 父类字段 ===
    messages: Optional[list]
    next_agent: Optional[str]
    current_agent: Optional[str]
    error: Optional[str]
    precheck_ok: Optional[bool]

    # === Installer 专属 ===
    install_phase: Optional[str]
    install_method: Optional[str]
    install_log: Optional[str]
    failed_packages: Optional[List[str]]
    skipped_packages: Optional[List[str]]
    install_status: Optional[str]
    retry_count: Optional[int]
    max_retries: Optional[int]
    current_index_url: Optional[str]
    extra_index_url: Optional[str]
    skip_install: Optional[bool]
    network_ok: Optional[bool]
    requirements_content: Optional[str]
    packages: Optional[List[str]]
    current_requirements_path: Optional[str]
    fix_strategy: Optional[str]
    verify_ok: Optional[bool]
    heavy_deps: Optional[List[Dict[str, Any]]]

    # === LLM 决策层字段 ===
    llm_analyzed: Optional[bool]
    llm_action: Optional[str]
    llm_reason: Optional[str]


# ============================================================
# 常量
# ============================================================
MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.org/simple",
]

CRITICAL_PACKAGES = {"torch", "vllm", "transformers", "numpy"}


# ============================================================
# 辅助函数
# ============================================================
def _exec_in_container(container_name: str, cmd: list, timeout: int = 10) -> tuple:
    full_cmd = ["docker", "exec", container_name] + cmd
    try:
        result = subprocess.run(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def _pkg_name(line: str) -> str:
    return line.strip().split("==")[0].split(">=")[0].split("<=")[0].strip()


def _parse_failed_packages_from_log(stderr: str) -> List[str]:
    failed = set()
    patterns = [
        r"Could not find a version that satisfies the requirement\s+([a-zA-Z0-9_.-]+)",
        r"No matching distribution found for\s+([a-zA-Z0-9_.-]+)",
        r"Failed to download\s+([a-zA-Z0-9_.-]+)",
        r"Failed to build\s+([a-zA-Z0-9_.-]+)",
        r"No package\s+([a-zA-Z0-9_.-]+)\s+found",
        r'Building wheel for\s+([a-zA-Z0-9_.-]+)',
        r'error: failed to build\s+([a-zA-Z0-9_.-]+)',
        r'Could not build wheels for\s+([a-zA-Z0-9_.-]+)',
        r'× Building wheel for\s+([a-zA-Z0-9_.-]+)',
        r'Running setup.py install for\s+([a-zA-Z0-9_.-]+)',
        r'Building\s+([a-zA-Z0-9_.-]+)==',
        r'×\s+Building wheel for\s+([a-zA-Z0-9_.-]+)',
        r"Because there is no version of\s+([a-zA-Z0-9_.-]+)==",
        r"Because only the following versions of\s+([a-zA-Z0-9_.-]+)\s+are available",
        r"Because\s+([a-zA-Z0-9_.-]+)==[\d\.+]+ depends on",
    ]
    for pat in patterns:
        for match in re.finditer(pat, stderr, re.IGNORECASE):
            pkg = match.group(1).strip()
            if pkg:
                failed.add(pkg)
    return list(failed)


# ============================================================
# LLM 错误分析层（真实 Tool Calling 闭环）
# ============================================================
def _llm_analyze_error(state: InstallerState) -> dict:
    error = state.get("error", "")
    log = state.get("install_log", "")
    reqs = state.get("requirements_content", "")
    failed = state.get("failed_packages", [])

    print(f"\n🧠 [Installer LLM] 分析安装错误...")

    system_prompt = """你是 Python 依赖安装专家，擅长分析 uv/pip 安装错误。
你绑定了两个工具：
- web_search: 联网搜索包的安装文档和常见错误
- manage_file: 修改 requirements 文件

请分析错误日志，输出 JSON 决策：
{"action": "skip_packages"|"switch_mirror"|"abort", "packages":[], "reason":"", "index_url":""}

规则：
- 如果是编译失败（cmake/gcc/nvcc 相关）→ abort
- 如果是版本冲突/包不存在 → skip_packages
- 如果是网络超时 → switch_mirror
- 不确定时 → abort，不要瞎猜"""

    user_prompt = f"""当前安装失败，请分析：

【错误日志】
{log[-1500:]}

【失败的包】
{failed}

【当前 requirements（前20行）】
{reqs[:600]}

请给出修复策略。"""

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt)
    ]

    try:
        llm = _get_installer_llm_with_tools()

        # ========== 第一轮：LLM 决定要不要搜索 ==========
        response = llm.invoke(messages)

        # ========== 如果有 tool_calls，执行工具并回传 ==========
        if hasattr(response, 'tool_calls') and response.tool_calls:
            print(f"🔧 [Installer LLM] 调用工具: {[tc['name'] for tc in response.tool_calls]}")

            tool_messages = []
            for tc in response.tool_calls:
                tool_name = tc.get('name', '')
                tool_id = tc.get('id', 'call_1')
                args = tc.get('args', tc.get('arguments', {}))

                if tool_name == 'web_search':
                    query = args.get('query', 'Python package install error solution')
                    # 执行真实 DuckDuckGo 搜索
                    search_result = web_search.invoke(query)
                    tool_messages.append(ToolMessage(
                        content=str(search_result),
                        name=tool_name,
                        tool_call_id=tool_id,
                    ))
                    print(f"   🔍 搜索 '{query}' 完成，结果长度: {len(str(search_result))}")

                elif tool_name == 'manage_file':
                    op = args.get('operation', 'write')
                    path = args.get('file_path', '/workspace/requirements_agent.txt')
                    tool_messages.append(ToolMessage(
                        content=f"文件操作已记录: {op} {path}",
                        name=tool_name,
                        tool_call_id=tool_id,
                    ))

            # ========== 第二轮：工具结果回传给 LLM ==========
            messages.extend([response] + tool_messages)
            final_response = llm.invoke(messages)
            content = final_response.content
            print(f"🧠 [Installer LLM] 基于搜索结果做出最终决策")
        else:
            content = response.content
            print(f"🧠 [Installer LLM] 未调用工具，直接决策")

        # ========== 解析 JSON 决策 ==========
        json_match = re.search(r'\{.*?\}', content, re.DOTALL)

        action = "abort"
        packages = []
        reason = "LLM 未给出明确策略"
        index_url = ""

        if json_match:
            try:
                decision = json.loads(json_match.group())
                action = decision.get("action", "abort")
                packages = decision.get("packages", [])
                reason = decision.get("reason", "")
                index_url = decision.get("index_url", "")
            except json.JSONDecodeError:
                pass

        print(f"🧠 [Installer LLM] 决策: {action}, 原因: {reason[:100]}")

        return {
            "llm_analyzed": True,
            "llm_action": action,
            "llm_reason": reason,
            "failed_packages": packages if action == "skip_packages" else [],
            "current_index_url": index_url if action == "switch_mirror" else None,
            "messages": [AIMessage(content=f"🧠 LLM 分析: {action}, {reason}")],
        }

    except Exception as e:
        print(f"⚠️ [Installer LLM] 分析失败（网络/搜索不可用）: {e}")
        return {
            "llm_analyzed": True,
            "llm_action": "fallback",
            "llm_reason": f"LLM/搜索异常: {e}",
        }


# ============================================================
# 节点 0：预检
# ============================================================
def precheck_node(state: InstallerState) -> dict:
    timestamps = state.get("timestamps", {})
    timestamps["installer_start"] = time.time()
    container_name = state.get("container_name", "dyfo-container")
    heavy = state.get("heavy_deps", []) or state.get("heavy_packages", [])
    
    if heavy:
        print("\n" + "=" * 65)
        print("⚠️  [Installer] 检测到以下包可能需要 CUDA/系统编译，将尝试自动安装")
        for item in heavy:
            if isinstance(item, dict):
                name = item.get("name", "unknown")
                meta = item.get("meta", {})
            else:
                name = getattr(item, "name", "unknown")
                meta = getattr(item, "meta", {})
            if not isinstance(meta, dict):
                meta = {}
            print(f"\n   📦 {name}")
            print(f"      类别 : {meta.get('category', 'unknown')}")
            print(f"      原因 : {meta.get('reason', '')}")
            print(f"      提示 : {meta.get('fix_hint', '若自动安装失败，将提供手动处理方案')}")
        print("\n   这些包已进入自动安装流程。")
        print("=" * 65 + "\n")

    result = run_precheck(state)

    if not result.get("precheck_ok"):
        issues = "; ".join(result.get("issues", []))
        return {
            "precheck_ok": False,
            "install_status": "failed",
            "install_phase": "init",
            "error": issues,
            "retry_count": 0,
            "fix_strategy": "abort",
            "skipped_packages": [],
            "messages": [AIMessage(content=f"❌ Installer 预检失败: {issues}")],
            **result.get("updates", {}),
            "timestamps": timestamps,
        }

    updates = result.get("updates", {})

    # === 在这里插入 P1 版本对齐 ===
    pytorch_cuda_version = state.get("pytorch_cuda_version", "12.1")
    framework = state.get("framework", "")
    if framework in ("pytorch", "tensorflow", "jax") and pytorch_cuda_version:
        cuda_ver_flat = pytorch_cuda_version.replace(".", "")
        pytorch_index = f"https://download.pytorch.org/whl/cu{cuda_ver_flat}"
        updates["current_index_url"] = pytorch_index
        updates["extra_index_url"] = "https://pypi.tuna.tsinghua.edu.cn/simple"
        print(f"[Installer] PyTorch CUDA 版本对齐: index-url={pytorch_index}, extra=tsinghua")

    return {
        "precheck_ok": True,
        "install_phase": "init",
        "install_status": "pending",
        "install_method": updates.get("install_method", "uv"),
        "extra_index_url": updates.get("extra_index_url", ""),
        "current_index_url": updates.get("current_index_url", "https://pypi.tuna.tsinghua.edu.cn/simple"),
        "install_log": "",
        "failed_packages": [],
        "skipped_packages": [],
        "retry_count": updates.get("retry_count", 0),
        "max_retries": updates.get("max_retries", 3),
        "skip_install": updates.get("skip_install", False),
        "network_ok": updates.get("network_ok", True),
        "requirements_content": updates.get("requirements_content", ""),
        "packages": updates.get("packages", []),
        "current_requirements_path": "/workspace/requirements_agent.txt",
        "error": "",
        "verify_ok": True,
        "messages": [AIMessage(content="✅ Installer 预检通过")],
        **updates,
    }


# ============================================================
# 节点 1：安装（内联执行 uv pip install，带实时输出）
# ============================================================
def install_node(state: InstallerState) -> dict:
    container_name = state.get("container_name", "dyfo-container")
    req_path = state.get("current_requirements_path", "/workspace/requirements_agent.txt")
    index_url = state.get("current_index_url", "https://pypi.tuna.tsinghua.edu.cn/simple")
    extra_index_url = state.get("extra_index_url", "")
    proxy_url = state.get("git_proxy", "")
    is_foreign_source = "pypi.org" in (index_url or "")

    print(f"\n🔧 [Installer] 开始安装 (retry={state.get('retry_count', 0)})")
    print(f"DEBUG: index_url={index_url}, req={req_path}")

    cmd = ["docker", "exec"]
    if proxy_url and not is_foreign_source:
        cmd.extend([
            "-e", "HTTP_PROXY=",
            "-e", "HTTPS_PROXY=",
            "-e", "http_proxy=",
            "-e", "https_proxy=",
        ])
    cmd.extend(["-e", "UV_HTTP_TIMEOUT=1800"])
    cmd.extend([
        container_name,
        "uv", "pip", "install",
        "--system",
        "--index-strategy", "unsafe-best-match",
        "-r", req_path,
    ])
    if extra_index_url:
        cmd.extend(["--extra-index-url", extra_index_url])
    if index_url:
        cmd.extend(["--index-url", index_url])

    print(f"[Installer] 执行: {' '.join(cmd)}")
    print("[Installer] 安装可能需要 5-15 分钟，请耐心等待...")

    try:
        full_output = deque(maxlen=500)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
        for line in proc.stdout:
            print(line, end="")
            full_output.append(line)

        rc = proc.wait(timeout=1800)
        out = "".join(full_output)
        err = ""

    except subprocess.TimeoutExpired:
        proc.kill()
        print(f"\n[INSTALLER DEBUG] ❌ 命令超时（1800秒）")
        return {
            "install_phase": "fix",
            "install_status": "failed",
            "failed_packages": [],
            "error": "安装命令超时（1800秒）",
            "install_log": "命令超时（1800秒）",
            "verify_ok": False,
        }
    except Exception as e:
        print(f"\n[INSTALLER DEBUG] ❌ 异常: {e}")
        return {
            "install_phase": "fix",
            "install_status": "failed",
            "failed_packages": [],
            "error": str(e),
            "install_log": str(e),
            "verify_ok": False,
        }

    if rc == 0:
        # 第三类：项目本身含编译扩展，尝试 uv pip install -e .
        if state.get("needs_project_build"):
            print("\n🔧 [Installer] 检测到项目含编译扩展，尝试执行 uv pip install -e .")
            build_cmd = [
                "docker", "exec", container_name,
                "bash", "-c",
                "cd /workspace && python -m pip install --upgrade setuptools wheel && python -m pip install -e . --no-build-isolation"
            ]
            try:
                build_proc = subprocess.Popen(
                    build_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                )
                build_output = []
                for line in build_proc.stdout:
                    print(line, end="")
                    build_output.append(line)
                
                build_rc = build_proc.wait(timeout=3600)
                build_out = "".join(build_output)
                
                if build_rc == 0:
                    print("✅ [Installer] 项目本身编译安装成功")
                    return {
                        "install_phase": "verifying",
                        "install_status": "installed",
                        "install_log": out[:1000] + "\n[Project Build] " + build_out[:500],
                        "failed_packages": [],
                        "verify_ok": True,
                        "messages": [AIMessage(content="✅ 第三方依赖安装完成，项目本身编译安装成功，准备验证导入")],
                    }
                else:
                    print("⚠️ [Installer] 项目编译失败，进入错误处理")
                    return {
                        "install_phase": "fix",
                        "install_status": "failed",
                        "failed_packages": [],
                        "error": build_out[-2000:],
                        "install_log": out[:1000] + "\n[Project Build ERROR] " + build_out[-2000:],
                        "verify_ok": False,
                    }
            except subprocess.TimeoutExpired:
                build_proc.kill()
                print("⚠️ [Installer] 项目编译超时（1小时）")
                return {
                    "install_phase": "fix",
                    "install_status": "failed",
                    "failed_packages": [],
                    "error": "项目源码编译超时（1小时），可能是编译型 CUDA 扩展",
                    "install_log": out[:1000] + "\n[Project Build TIMEOUT]",
                    "verify_ok": False,
                }
            except Exception as e:
                return {
                    "install_phase": "fix",
                    "install_status": "failed",
                    "failed_packages": [],
                    "error": f"项目编译异常: {str(e)}",
                    "install_log": out[:1000] + f"\n[Project Build EXCEPTION] {str(e)}",
                    "verify_ok": False,
                }
        
        # 第一类 / 第二类：不需要编译项目本身
        return {
            "install_phase": "verifying",
            "install_status": "installed",
            "install_log": out[:1000],
            "failed_packages": [],
            "verify_ok": True,
            "messages": [AIMessage(content="✅ uv 安装完成，准备验证导入")],
        }

    stderr_lower = out.lower()

    if "is not running" in stderr_lower or "cannot exec" in stderr_lower:
        return {
            "install_phase": "fix",
            "install_status": "failed",
            "failed_packages": [],
            "error": out[-2000:],
            "install_log": out[-2000:],
            "verify_ok": False,
        }

    if "no virtual environment found" in stderr_lower:
        return {
            "install_phase": "fix",
            "install_status": "failed",
            "failed_packages": [],
            "error": "容器内无 Python 虚拟环境，且 uv 拒绝安装到系统 Python。请检查 Dockerfile 是否配置 venv，或允许 --system 安装。",
            "install_log": out[-2000:],
            "verify_ok": False,
        }

    if "depends on" in stderr_lower and "incompatible" in stderr_lower:
        failed = _parse_failed_packages_from_log(out)
        return {
            "install_phase": "fix",
            "install_status": "failed",
            "failed_packages": failed,
            "error": out[-2000:],
            "install_log": out[-2000:],
            "verify_ok": False,
        }

    if any(k in stderr_lower for k in [
        "runtimeerror", "cmake", "build environment", "failed to build",
        "could not build wheels", "compilation", "gcc", "g++", "cython",
        "no such file or directory", "header file", "error: command 'gcc'"
    ]):
        failed = _parse_failed_packages_from_log(out)
        return {
            "install_phase": "fix",
            "install_status": "failed",
            "failed_packages": failed,
            "error": out[-2000:],
            "install_log": out[-2000:],
            "verify_ok": False,
        }

    if any(k in stderr_lower for k in ["timeout", "timed out", "network", "connection", "ssl"]):
        return {
            "install_phase": "fix",
            "install_status": "failed",
            "failed_packages": [],
            "error": out[-2000:],
            "install_log": out[-2000:],
            "verify_ok": False,
        }

    failed = _parse_failed_packages_from_log(out)
    return {
        "install_phase": "fix",
        "install_status": "failed",
        "failed_packages": failed,
        "error": out[-2000:],
        "install_log": out[-2000:],
        "verify_ok": False,
    }


# ============================================================
# 节点 2：验证（内联执行 import）
# ============================================================
def verify_node(state: InstallerState) -> dict:
    container_name = state.get("container_name", "dyfo-container")
    req_path = state.get("current_requirements_path", "/workspace/requirements_agent.txt")
    # === C类项目：验证项目本身是否编译成功 ===
    if state.get("needs_project_build"):
        from pathlib import Path
        import re
        
        # 从项目路径推断包名（如 depth-anything-v2 → depth_anything_v2）
        project_name = Path(state.get("project_path_win", "")).name
        package_name = re.sub(r'[^a-zA-Z0-9]', '_', project_name).strip('_').lower()
        package_name = re.sub(r'_+', '_', package_name)  # 合并多个下划线
        
        print(f"\n🔍 [Installer] 验证项目本身编译结果 (尝试 import {package_name})...")
        script = f"import {package_name}\nprint('ok')"
        rc, out, err = _exec_in_container(
            container_name, ["python3", "-c", script], timeout=30
        )
        if rc == 0:
            print(f"[INSTALLER DEBUG]   ✓ {package_name} 导入成功")
            return {
                "install_phase": "done",
                "install_status": "success",
                "verify_ok": True,
                "failed_packages": [],
                "messages": [AIMessage(content=f"✅ 项目本身编译安装成功，{package_name} 验证通过")],
            }
        else:
            # fallback：pip list 检查（包名和目录名可能不完全一致）
            rc2, out2, _ = _exec_in_container(
                container_name, ["python3", "-m", "pip", "list"], timeout=10
            )
            if rc2 == 0 and package_name.replace("_", "-") in out2.lower():
                print(f"[INSTALLER DEBUG]   ✓ {package_name} 已在 pip list 中（import 名可能不同）")
                return {
                    "install_phase": "done",
                    "install_status": "success",
                    "verify_ok": True,
                    "failed_packages": [],
                    "messages": [AIMessage(content=f"✅ 项目本身编译安装成功（pip list 确认）")],
                }
            
            print(f"[INSTALLER DEBUG]   ✗ {package_name} 验证失败: {err[:200]!r}")
            return {
                "install_phase": "done",
                "install_status": "partial",
                "failed_packages": [package_name],
                "verify_ok": False,
                "messages": [AIMessage(content=f"⚠️ 第三方依赖通过，但项目本身 {package_name} 验证失败: {err[:200]}")],
            }
    rc, content, err = _exec_in_container(container_name, ["cat", req_path], timeout=10)
    if rc != 0:
        return {
            "install_phase": "done",
            "install_status": "failed",
            "verify_ok": False,
            "messages": [AIMessage(content=f"❌ 验证阶段无法读取 requirements: {err[:200]}")],
            "failed_packages": [],
        }

    # ========== 新增：解析 # import: 注释 ==========
    import_map = {}
    packages = []
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("# import:")
        pkg = parts[0].strip()
        packages.append(pkg)
        if len(parts) > 1:
            import_map[pkg] = parts[1].strip()
    # ==========

    # Scanner 2.1: 跳过 heavy 包
    # 新策略：所有包都验证，包括历史上标记为 heavy 的包
    # Installer 已尝试安装所有包，装上的就验证，没装上的走 retry 逻辑

    print(f"\n🔍 [Installer] 验证 {len(packages)} 个包...")

    # 只保留极少数硬编码（PIL 这种确实差异大的）
    hardcoded_map = {
        "opencv-python-headless": "cv2",
        "opencv-python": "cv2",
        "pillow": "PIL",
        "scikit-learn": "sklearn",
        "scikit-image": "skimage",
        "pyyaml": "yaml",
        "python-dateutil": "dateutil",
        "attrs": "attrs",
        "huggingface-hub": "huggingface_hub",
        "protobuf": "google.protobuf",
    }

    failed = []
    for pkg in packages:
        clean_pkg = _pkg_name(pkg)
        
        # 1. 优先 Scanner 注释里的 import_name
        module_name = import_map.get(clean_pkg)
        
        # 2. fallback 硬编码
        if not module_name:
            module_name = hardcoded_map.get(clean_pkg.lower())
        
        # 3. 最后 fallback 截断
        if not module_name:
            module_name = clean_pkg.split("-")[0].split("_")[0]

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
            ["python3", "-c", script],
            timeout=60,
        )

        if rc == 0:
            print(f"[INSTALLER DEBUG]   ✓ {clean_pkg} {out.strip()}")
        else:
            failed.append(clean_pkg)
            print(f"[INSTALLER DEBUG]   ✗ {clean_pkg} {err[:200]!r}")

    if not failed:
        return {
            "install_phase": "done",
            "install_status": "success",
            "verify_ok": True,
            "failed_packages": [],
            "messages": [AIMessage(content=f"✅ 全部 {len(packages)} 个包验证通过")],
        }

    return {
        "install_phase": "done",
        "install_status": "partial",
        "failed_packages": failed,
        "verify_ok": True,
        "messages": [AIMessage(content=f"⚠️ {len(failed)} 个包验证失败: {failed}")],
    }


# ============================================================
# 节点 3：修复（规则引擎 + LLM 决策层）
# ============================================================
def retry_node(state: InstallerState) -> dict:
    retries = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)
    error = state.get("error", "")
    error_lower = error.lower()
    failed = state.get("failed_packages", [])
    current_url = state.get("current_index_url", "https://pypi.org/simple")
    container_name = state.get("container_name", "dyfo-container")

    print(f"\n🔧 [Installer] 修复策略 (retry={retries}/{max_retries})")
    print(f"DEBUG 错误类型: {error[:100]}")

    if retries >= max_retries:
        return {
            "retry_count": retries,
            "fix_strategy": "abort",
            "install_phase": "done",
            "error": f"安装失败，已达最大重试次数: {error}",
        }

    # ==========================================
    # LLM 决策层：第一次修复时让 LLM 分析
    # ==========================================
    if retries == 0 and not state.get("llm_analyzed"):
        llm_result = _llm_analyze_error(state)
        
        if llm_result.get("llm_action") == "skip_packages":
            pkgs = llm_result.get("failed_packages", [])
            if pkgs:
                print(f"[Installer] LLM 建议剔包: {pkgs}")
                return _do_filter_packages(state, pkgs, container_name)
        
        elif llm_result.get("llm_action") == "switch_mirror":
            suggested_url = llm_result.get("current_index_url")
            if suggested_url:
                print(f"[Installer] LLM 建议换源: {suggested_url}")
                return _do_switch_mirror(state, current_url, suggested_url)
        
        elif llm_result.get("llm_action") == "abort":
            return {
                "retry_count": retries,
                "fix_strategy": "abort",
                "install_phase": "done",
                "error": f"LLM 判断无法修复: {llm_result.get('llm_reason', '')}",
                "messages": llm_result.get("messages", []),
            }
        
        print("[Installer] LLM 未给出明确策略，降级到规则引擎")
    # ==========================================

    # 规则引擎兜底
    if any(k in error for k in ["容器执行异常", "无虚拟环境", "is not running", "cannot exec"]):
        return {
            "retry_count": retries,
            "fix_strategy": "abort",
            "install_phase": "done",
            "error": f"容器/环境错误，无法修复: {error[:200]}",
        }
    
        # 新增：CUDA 编译环境缺失
    if any(k in error_lower for k in [
        "nvcc not found", "nvcc: not found", "cuda_home not set",
        "cuda_home not found", "cannot find -lcudart", "cuda_runtime.h",
        "no such file or directory.*cuda", "unable to find the nvcc compiler",
        "nvcc is required", "cuda is required",
        "could not find any cuda installation",
    ]):
        if not failed:
            failed = _parse_failed_packages_from_log(error)
        return {
            "retry_count": retries,
            "fix_strategy": "needs_cuda_compile",
            "install_phase": "done",
            "install_status": "needs_cuda_compile",  # ← 必须加，否则 extract_node 识别不到
            "error": f"CUDA 编译环境缺失（缺 nvcc/CUDA 头文件），包 {failed or 'unknown'} 需要源码编译。",
            "failed_packages": failed or [],
        }

    # 新增：项目本身编译失败（needs_project_build）
    if state.get("needs_project_build") and any(k in error_lower for k in [
        "runtimeerror", "cmake", "build environment", "failed to build",
        "could not build wheels", "compilation", "gcc", "g++", "cython",
        "nvcc not found", "cuda_home not set", "error: command"
    ]):
        if not failed:
            failed = _parse_failed_packages_from_log(error)
        return {
            "retry_count": retries,
            "fix_strategy": "needs_project_build",
            "install_phase": "done",
            "install_status": "needs_project_build",
            "error": f"项目本身（含 CUDA/C++ 扩展）编译失败。可能原因：1) 容器 CUDA 版本与 PyTorch 不匹配 2) 缺少系统编译依赖 3) 编译超时。",
            "failed_packages": failed or [],
        }

    if any(k in error_lower for k in [
        "runtimeerror", "cmake", "build environment", "failed to build",
        "could not build wheels", "compilation", "gcc", "g++", "cython"
    ]):
        if not failed:
            failed = _parse_failed_packages_from_log(error)
        req_packages = state.get("packages", [])
        req_set = {p.lower() for p in req_packages}
        if req_set:
            failed = [p for p in failed if p.lower() in req_set]
        if not failed:
            build_matches = re.findall(r'[Bb]uilding\s+([a-zA-Z0-9_.-]+)==[\d\.]+', error)
            if build_matches:
                failed = [build_matches[-1]]
        pkg_str = f"涉及包: {failed}" if failed else "无法定位具体包"
        return {
            "retry_count": retries,
            "fix_strategy": "abort",
            "install_phase": "done",
            "error": f"容器内编译环境已就绪，但 {pkg_str} 仍编译失败。通常是 CUDA 版本不匹配或代码与当前 gcc 版本不兼容，请手动检查。",
        }

    if any(k in error_lower for k in ["depends on", "incompatible", "unsatisfiable", "依赖版本冲突"]):
        if not failed:
            failed = _parse_failed_packages_from_log(error)
        if failed:
            print(f"[Installer] 检测到版本冲突，直接剔包: {failed}")
            return _do_filter_packages(state, failed, container_name)
        else:
            return {
                "retry_count": retries,
                "fix_strategy": "abort",
                "install_phase": "done",
                "error": f"依赖冲突但无法定位冲突包: {error[:200]}",
            }

    if any(k in error_lower for k in ["timeout", "timed out", "network", "connection", "ssl", "网络/超时"]):
        return _do_switch_mirror(state, current_url)

    if any(k in error_lower for k in ["no matching distribution", "could not find", "no version of"]):
        if failed:
            print(f"[Installer] 包不存在，剔包: {failed}")
            return _do_filter_packages(state, failed, container_name)
        else:
            return {
                "retry_count": retries,
                "fix_strategy": "abort",
                "install_phase": "done",
                "error": f"包无法获取但无法定位: {error[:200]}",
            }

    if failed:
        print(f"[Installer] 未知错误，尝试剔包: {failed}")
        return _do_filter_packages(state, failed, container_name)

    return {
        "retry_count": retries,
        "fix_strategy": "abort",
        "install_phase": "done",
        "error": f"无法修复的安装错误: {error}",
    }


# ========== 辅助函数：换源（支持 LLM 建议的 URL）==========
def _do_switch_mirror(state: InstallerState, current_url: str, next_url: Optional[str] = None) -> dict:
    retries = state.get("retry_count", 0)
    
    if next_url and next_url != current_url:
        print(f"[Installer] LLM 建议换源: {current_url} → {next_url}")
        return {
            "retry_count": retries + 1,
            "fix_strategy": "retry",
            "current_index_url": next_url,
            "failed_packages": [],
            "error": f"LLM 建议换源: {next_url}",
            "verify_ok": False,
            "install_phase": "init",
        }
    
    if current_url in MIRRORS:
        idx = MIRRORS.index(current_url)
        if idx + 1 < len(MIRRORS):
            next_url = MIRRORS[idx + 1]
            print(f"[Installer] 轮询换源: {current_url} → {next_url}")
            return {
                "retry_count": retries + 1,
                "fix_strategy": "retry",
                "current_index_url": next_url,
                "failed_packages": [],
                "error": f"轮询换源: {next_url}",
                "verify_ok": False,
                "install_phase": "init",
            }
    
    return {
        "retry_count": retries,
        "fix_strategy": "abort",
        "install_phase": "done",
        "error": "无可用镜像",
    }


# ========== 辅助函数：剔包 ==========
def _do_filter_packages(state: InstallerState, failed: List[str], container_name: str) -> dict:
    retries = state.get("retry_count", 0)
    content = state.get("requirements_content", "")
    lines = content.split("\n")

    failed_set = set(_pkg_name(f) for f in failed)
    skipped_set = set(_pkg_name(f) for f in state.get("skipped_packages", []))
    all_excluded = failed_set | skipped_set

    filtered = [line for line in lines if _pkg_name(line) not in all_excluded]

    if not filtered:
        return {
            "retry_count": retries,
            "fix_strategy": "abort",
            "install_phase": "done",
            "error": f"所有包均无法安装，失败包: {list(all_excluded)}",
        }

    temp_path = "/workspace/requirements_filtered.txt"
    write_script = (
        f"lines = {repr(filtered)}\n"
        f"with open('{temp_path}', 'w') as f:\n"
        "    f.write('\\n'.join(lines) + '\\n')"
    )

    rc, _, err = _exec_in_container(container_name, ["python3", "-c", write_script], timeout=10)
    if rc != 0:
        return {
            "retry_count": retries,
            "fix_strategy": "abort",
            "install_phase": "done",
            "error": f"写入临时 requirements 失败: {err}",
        }

    filtered_pkg_names = [_pkg_name(line) for line in filtered if line.strip()]
    old_skipped = state.get("skipped_packages", [])
    new_skipped = list(set(old_skipped + [_pkg_name(f) for f in failed]))

    print(f"[Installer] 剔包重试，移除 {len(failed_set)} 个包，保留 {len(filtered_pkg_names)} 个")
    print(f"[Installer] 历史剔除包: {new_skipped}")

    return {
        "retry_count": retries + 1,
        "fix_strategy": "retry",
        "current_requirements_path": temp_path,
        "packages": filtered_pkg_names,
        "failed_packages": [],
        "skipped_packages": new_skipped,
        "requirements_content": "\n".join(filtered),
        "error": f"剔包重试，移除: {list(failed_set)}",
        "verify_ok": False,
        "install_phase": "init",
    }


# ============================================================
# 节点 4：提取结果（统一出口）
# ============================================================
def extract_node(state: InstallerState) -> dict:
    timestamps = state.get("timestamps", {})
    timestamps["installer_end"] = time.time()
    
    # === 计时汇总（各阶段纯执行时间之和，不含用户等待）===
    total = 0
    for phase in ["git", "scanner", "docker_builder", "installer"]:
        start = timestamps.get(f"{phase}_start")
        end = timestamps.get(f"{phase}_end")
        if start and end:
            total += (end - start)
    
    print(f"\n{'='*60}")
    print(f"⏱️ [总耗时] {total:.1f}s  ({total/60:.1f} min)")
    for phase in ["git", "scanner", "docker_builder", "installer"]:
        start = timestamps.get(f"{phase}_start")
        end = timestamps.get(f"{phase}_end")
        if start and end:
            print(f"⏱️ [{phase:14s}] {end-start:6.1f}s  ({(end-start)/60:.1f} min)")
    print(f"{'='*60}")

        # 双重保险：优先用 fix_strategy 判断，防止 install_status 字段丢失
    if state.get("fix_strategy") == "needs_cuda_compile" or state.get("install_status") == "needs_cuda_compile":
        failed_pkgs = state.get("failed_packages", [])
        container = state.get("container_name", "xxx-container")
        image = state.get("docker_image", "xxx:latest")
        msg = (
            f"❌ CUDA 编译环境缺失，以下包需要源码编译但容器内缺少 nvcc/CUDA 头文件：{failed_pkgs}\n\n"
            f"解决方案（推荐方案 A）：\n"
            f"A. 删除当前镜像和容器，使用 devel 基础镜像重新 build：\n"
            f"   1. 在 PowerShell 执行：\n"
            f"      docker rm -f {container}\n"
            f"      docker rmi {image}\n"
            f"   2. 重新运行 main.py，在 Scanner 阶段输入：m: 使用 devel 镜像\n"
            f"   （当前 Agent 尚未支持自动切换 devel，需手动重跑）\n\n"
            f"B. 手动进入容器安装 CUDA toolkit（不推荐，易版本不匹配）：\n"
            f"   docker exec -it {container} bash\n"
            f"   apt-get update && apt-get install -y nvidia-cuda-toolkit-12-1\n"
        )
        return {
            "install_status": "needs_cuda_compile",
            "failed_packages": failed_pkgs,
            "messages": [AIMessage(content=msg)],
            "next_agent": "finish",
            "current_agent": "installer",
        }
    
    # 新增：项目本身编译失败
    if state.get("fix_strategy") == "needs_project_build" or state.get("install_status") == "needs_project_build":
        container = state.get("container_name", "xxx-container")
        msg = (
            "❌ 项目本身（含 CUDA/C++ 扩展）编译失败。\n\n"
            "可能原因：\n"
            "  1. 容器 CUDA 版本与 PyTorch 不匹配（如 PyTorch cu130 + nvcc 12.1）\n"
            "  2. 缺少系统编译依赖（cmake, ninja, g++）\n"
            "  3. 编译时间超过 1 小时被中断\n\n"
            "手动修复方案：\n"
            f"   docker exec -it {container} bash\n"
            f"   cd /workspace && uv pip install -e . --no-build-isolation --system\n\n"
            "如需更换 CUDA 版本，请删除容器/镜像后重跑 main.py。"
        )
        return {
            "install_status": "needs_project_build",
            "failed_packages": state.get("failed_packages", []),
            "messages": [AIMessage(content=msg)],
            "next_agent": "finish",
            "current_agent": "installer",
        }

    status = state.get("install_status", "unknown")
    failed = state.get("failed_packages", [])
    skipped = state.get("skipped_packages", [])
    log = state.get("install_log", "")[:500]
    heavy = state.get("heavy_deps", [])

    heavy_notice = ""
    if heavy:
        lines = ["", "📦 以下包需手动处理（系统级依赖，已从自动安装剔除）："]
        for item in heavy:
            lines.append(f"   • {item['name']} —— {item['meta']['reason']}")
        lines.append(f"💡 手动安装示例: {heavy[0]['meta'].get('fix_hint', '见项目文档')}")
        heavy_notice = "\n".join(lines)

    failed_from_comment = []
    raw_content = state.get("requirements_content", "")
    m = re.search(r"# 以下包解析失败.*：(.+)", raw_content)
    if m:
        failed_from_comment = [p.strip() for p in m.group(1).split(",") if p.strip()]

    real_failed = set(failed) | set(skipped)
    comment_failed = set(failed_from_comment)
    all_missing = real_failed | comment_failed
    critical_missing = real_failed & CRITICAL_PACKAGES

    if critical_missing:
        msg = f"❌ 核心包缺失: {sorted(critical_missing)}"
        if skipped:
            msg += f"（被剔除: {skipped}）"
        if failed:
            msg += f"（验证失败: {failed}）"
        msg += heavy_notice
        return {
            "install_status": "failed",
            "failed_packages": list(real_failed),
            "messages": [AIMessage(content=msg)],
            "next_agent": "finish",
            "current_agent": "installer",
        }

    if status == "failed" and not failed and not skipped and not failed_from_comment:
        msg = f"❌ 安装失败: {state.get('error', '')}"
        msg += heavy_notice
        return {
            "install_status": "failed",
            "failed_packages": [],
            "messages": [AIMessage(content=msg)],
            "next_agent": "finish",
            "current_agent": "installer",
        }

    elif status == "failed":
        if real_failed:
            msg = f"❌ 安装失败，实际失败包: {sorted(real_failed)}"
            failed_pkgs = list(real_failed)
        else:
            msg = f"❌ 安装失败"
            failed_pkgs = []
        if failed_from_comment:
            msg += f"。Scanner 注释包: {sorted(comment_failed)}"
        msg += f"。错误: {state.get('error', '')[:200]}"
        msg += heavy_notice
        return {
            "install_status": "failed",
            "failed_packages": failed_pkgs,
            "messages": [AIMessage(content=msg)],
            "next_agent": "finish",
            "current_agent": "installer",
        }

    if status == "partial" or skipped or failed_from_comment:
        msg_parts = ["⚠️ 依赖安装部分完成"]
        if skipped:
            msg_parts.append(f"被剔除包: {skipped}")
        if failed:
            msg_parts.append(f"验证失败包: {failed}")
        if failed_from_comment:
            msg_parts.append(f"Scanner 解析失败包: {failed_from_comment}")
        msg = "；".join(msg_parts) + heavy_notice
        return {
            "install_status": "partial",
            "failed_packages": list(real_failed),
            "messages": [AIMessage(content=msg)],
            "next_agent": "finish",
            "current_agent": "installer",
        }

    if status == "success":
        msg = "✅ 全部依赖安装并验证通过"
        msg += heavy_notice
        return {
            "install_status": "success",
            "failed_packages": [],
            "messages": [AIMessage(content=msg)],
            "next_agent": "finish",
            "current_agent": "installer",
        }

    msg = f"❓ 安装状态异常: {status!r}"
    msg += heavy_notice
    return {
        "install_status": "unknown",
        "failed_packages": list(all_missing),
        "messages": [AIMessage(content=msg)],
        "next_agent": "finish",
        "current_agent": "installer",
    }


# ============================================================
# 路由函数
# ============================================================
def precheck_router(state: InstallerState) -> str:
    if not state.get("precheck_ok"):
        return "extract"
    if state.get("skip_install"):
        return "verify"
    return "install"


def install_router(state: InstallerState) -> str:
    if not state.get("verify_ok"):
        return "fix"
    return "verify"


def verify_router(state: InstallerState) -> str:
    phase = state.get("install_phase")
    if phase == "done":
        return "extract"
    return "fix"


def retry_router(state: InstallerState) -> str:
    strategy = state.get("fix_strategy", "")
    if strategy == "retry":
        return "install"
    if strategy in ("needs_cuda_compile", "needs_project_build"):
        return "extract"
    return "extract"

# ============================================================
# 构建子图
# ============================================================
def create_installer_agent():
    workflow = StateGraph(InstallerState)

    workflow.add_node("precheck", precheck_node)
    workflow.add_node("install", install_node)
    workflow.add_node("verify", verify_node)
    workflow.add_node("retry", retry_node)
    workflow.add_node("extract", extract_node)

    workflow.set_entry_point("precheck")

    workflow.add_conditional_edges(
        "precheck",
        precheck_router,
        {"install": "install", "verify": "verify", "extract": "extract"},
    )

    workflow.add_conditional_edges(
        "install",
        install_router,
        {"verify": "verify", "fix": "retry"},
    )

    workflow.add_conditional_edges(
        "verify",
        verify_router,
        {"extract": "extract", "fix": "retry"},
    )

    workflow.add_conditional_edges(
        "retry",
        retry_router,
        {"install": "install", "extract": "extract"},
    )

    workflow.add_edge("extract", END)

    return workflow.compile()


installer_agent_node = create_installer_agent()