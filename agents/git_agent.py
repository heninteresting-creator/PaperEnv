"""
Git 专员子图 (Subgraph) —— Windows 本地克隆版
职责：调度 skills/git_clone 完成克隆，无需 WSL，不涉及任何 shell 命令字符串。
"""

import os
import shutil          # ← 确保有
import time            # ← 新增
import importlib
import subprocess
import json
from typing import Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from pipeline_state import PipelineState
from skill_loader import get_skill_system_prompt, get_skill_functions


# ---------- 状态定义 ----------
class GitAgentState(PipelineState, total=False):
    retry_count: Optional[int]
    verify_ok: Optional[bool]
    git_status: Optional[str]
    local_path: Optional[str]
    proxy_corrected: Optional[bool]
    error: Optional[str]


# ---------- 动态加载 Skill 组件 ----------
precheck_module = importlib.import_module("skills.git_clone.precheck")
run_precheck = precheck_module.run_precheck

git_tools = get_skill_functions("git_clone")
tool_node = ToolNode(git_tools)


# ---------- 动态加载完整性检查 ----------
git_functions_module = importlib.import_module("skills.git_clone.functions")
check_repo_integrity = git_functions_module.check_repo_integrity


# ---------- 安全执行器 ----------
def _safe_cli(command: str, timeout: int = 30) -> dict:
    """统一包装 run_cli 调用，确保返回 dict"""
    try:
        from tools import run_cli
        result = run_cli.invoke({"command": command, "timeout": timeout})
        if isinstance(result, dict):
            return result
        elif isinstance(result, str):
            return {"stdout": result, "stderr": "", "returncode": 0}
        else:
            return {"stdout": str(result), "stderr": "", "returncode": 0}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": 1}


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
    return _llm.bind_tools(git_tools)


# ==========================================
# 节点 0：预检（AgentMonitor）
# ==========================================
def precheck_node(state: GitAgentState) -> dict:
    timestamps = state.get("timestamps", {})
    if not timestamps.get("pipeline_start"):
        timestamps["pipeline_start"] = time.time()
    timestamps["git_start"] = time.time()
    # === 第一层：正常预检，提取参数 ===
    result = run_precheck(state)
    updates = result.get("updates", {})
    
    if not result.get("precheck_ok"):
        return {
            "precheck_ok": False,
            "error": "; ".join(result.get("issues", [])),
            "retry_count": 0,
            "verify_ok": False,
            "git_status": "failed",
            "local_path": "",
            "proxy_corrected": False,
            **updates,
            "timestamps": timestamps,
        }
    
    # === 第二层：从预检结果推断本地路径，自检仓库完整性 ===
    repo_url = updates.get("repo_url", state.get("repo_url", ""))
    target_root = updates.get("target_root", state.get("target_root", os.getcwd()))
    
    # 推断本地路径（和 skill_git_clone 逻辑一致）
    repo_name = repo_url.rstrip("/").split("/")[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]
    local_path = os.path.join(target_root, repo_name)
    
    print(f"[Git DEBUG] 推断路径: {local_path!r}, exists={os.path.exists(local_path)}")

    if os.path.exists(local_path):
        ok, reason = check_repo_integrity(local_path)
        if ok:
            print(f"✅ [Git 预检] 仓库已存在且完整，跳过克隆: {local_path}")
            return {
                "precheck_ok": True,
                "git_status": "success",
                "local_path": local_path,
                "project_path_win": local_path,
                "messages": [AIMessage(content=f"✅ 仓库已存在且完整，跳过克隆: {local_path}")],
                "next_agent": "scanner",
                "current_agent": "git",
                "retry_count": 0,
                "verify_ok": True,
                "error": "",
                **updates,
                "timestamps": timestamps,
            }
        else:
            # 目录存在但不完整，自动清理后重新克隆
            print(f"⚠️ [Git 预检] 仓库不完整 ({reason})，自动清理: {local_path}")
            shutil.rmtree(local_path, ignore_errors=True)
            if os.path.exists(local_path):
                subprocess.run(
                    ["cmd", "/c", "rmdir", "/s", "/q", local_path],
                    capture_output=True, check=False, timeout=10
                )
            if os.path.exists(local_path):
                try:
                    os.rename(local_path, local_path + "_old_" + str(int(time.time())))
                except Exception:
                    pass
    
    # === 第三层：仓库不存在或不完整，继续正常克隆流程 ===
    return {
        "precheck_ok": True,
        "repo_url": updates.get("repo_url", state.get("repo_url", "")),
        "target_root": updates.get("target_root", state.get("target_root", "")),
        "git_proxy": updates.get("git_proxy", state.get("git_proxy", "")),
        "retry_count": 0,
        "verify_ok": False,
        "git_status": "",
        "local_path": "",
        "proxy_corrected": False,
        "error": "",
        **updates,
        "timestamps": timestamps,
    }

# ==========================================
# 节点 1：LLM 决策
# ==========================================
def call_model(state: GitAgentState) -> dict:
    print(f"DEBUG git_tools loaded: {[t.name for t in git_tools] if git_tools else 'EMPTY'}")
    print("\n🧠 [Git 专员] 思考下一步...")
    repo_url = state.get("repo_url", "")
    target_root = state.get("target_root", "")
    proxy = state.get("git_proxy", "")

    extra = f"仓库 URL：{repo_url}\n目标根目录：{target_root}\n"
    if proxy:
        extra += f"代理地址：{proxy}\n"
    else:
        extra += "（未设置代理，若网络不通可提供代理地址）\n"
    extra += "请直接调用 skill_git_clone 工具，不要自行拼接任何 git 命令。"

    system_prompt = get_skill_system_prompt("git_clone", extra_context=extra)

    existing = state.get("messages", [])
    dialogs = []
    has_system = False
    for m in existing:
        if isinstance(m, SystemMessage):
            has_system = True
            dialogs.append(m)
        else:
            dialogs.append(m)

    if not has_system:
        dialogs.insert(0, SystemMessage(content=system_prompt))

    if not any(isinstance(m, HumanMessage) for m in dialogs):
        dialogs.append(HumanMessage(content=f"将仓库 {repo_url} 克隆到 {target_root}"))

    llm = _get_llm()
    response = llm.invoke(dialogs)

    if hasattr(response, 'tool_calls') and response.tool_calls:
        print(f"🔧 [Git 专员] 调用工具: {[tc['name'] for tc in response.tool_calls]}")
    
    print(f"DEBUG response type: {type(response)}")
    print(f"DEBUG response content: {repr(response.content[:300]) if response.content else 'EMPTY'}")
    print(f"DEBUG has tool_calls: {hasattr(response, 'tool_calls')}")
    if hasattr(response, 'tool_calls'):
        print(f"DEBUG tool_calls: {response.tool_calls}")
    print(f"DEBUG finish_reason: {getattr(response, 'response_metadata', {}).get('finish_reason', 'N/A')}")

    return {"messages": [response]}


# ==========================================
# 条件边：是否调用工具
# ==========================================
def should_continue(state: GitAgentState) -> str:
    last_msg = state["messages"][-1]
    if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
        return "tools"
    return "verify"


# ==========================================
# 节点 2：硬编码验证（Checkpoint）
# ==========================================
def verify_clone(state: GitAgentState) -> dict:
    # 提取 skill_git_clone 返回的 ToolMessage
    tool_msg = None
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, ToolMessage) and msg.name == "skill_git_clone":
            tool_msg = msg
            break

    if not tool_msg:
        return {"verify_ok": False, "error": "未检测到 skill_git_clone 调用记录"}

    try:
        result = json.loads(tool_msg.content)
    except json.JSONDecodeError:
        return {"verify_ok": False, "error": "工具返回非法 JSON"}

    if not result.get("success"):
        return {
            "verify_ok": False,
            "error": result.get("error", "克隆失败"),
            "local_path": result.get("local_path", "")
        }

    local_path = result.get("local_path", "")
    if not local_path:
        return {"verify_ok": False, "error": "克隆成功但路径为空"}

    # ← 修改：用 check_repo_integrity 替代原来的 .git 存在检查
    ok, reason = check_repo_integrity(local_path)
    if not ok:
        return {
            "verify_ok": False,
            "error": f"仓库完整性检查失败: {reason}",
            "local_path": local_path
        }

    return {"verify_ok": True, "local_path": local_path}


# ==========================================
# 节点 3：修复分支（AgentFix）—— 修改：删除逻辑与 functions.py 一致
# ==========================================
def fix_node(state: GitAgentState) -> dict:
    retries = state.get("retry_count", 0)
    error = state.get("error", "")
    error_lower = error.lower()

    if retries >= 3:
        return {
            "retry_count": retries,
            "fix_strategy": "abort",
            "error": "克隆/验证失败，已达最大重试次数。请检查网络或提供代理地址。"
        }

    strategy = "retry"
    extra_updates = {}
    local_path = state.get("local_path", "")

    # ← 修改：统一处理"已存在"和"完整性失败"，使用三层删除保障
    if "already exists" in error_lower or "无法清理" in error or "完整性检查失败" in error:
        if local_path and os.path.exists(local_path):
            # 第一层：shutil
            shutil.rmtree(local_path, ignore_errors=True)
            # 第二层：Windows 强制删除
            if os.path.exists(local_path):
                subprocess.run(
                    ["cmd", "/c", "rmdir", "/s", "/q", local_path],
                    capture_output=True,
                    check=False,
                    timeout=10
                )
            # 第三层：重命名占位
            if os.path.exists(local_path):
                try:
                    os.rename(local_path, local_path + "_old_" + str(int(time.time())))
                except Exception:
                    pass
        strategy = "retry"
        extra_updates["error"] = ""
    elif "timeout" in error_lower or "443" in error_lower or "could not resolve host" in error_lower:
        strategy = "abort"
        extra_updates["error"] = "网络不可达，请提供代理地址后重试。"
    elif "permission denied" in error_lower:
        strategy = "abort"
        extra_updates["error"] = "权限不足，请检查目标目录权限。"
    else:
        strategy = "retry"

    return {
        "retry_count": retries + 1,
        "fix_strategy": strategy,
        "error": extra_updates.get("error", error),
        "local_path": local_path
    }


# ==========================================
# 节点 4：提取结果（出口）
# ==========================================
def extract_result(state: GitAgentState) -> dict:
    timestamps = state.get("timestamps", {})
    timestamps["git_end"] = time.time()
    if timestamps.get("git_start"):
        elapsed = timestamps["git_end"] - timestamps["git_start"]
        print(f"\n⏱️ [Git] 阶段耗时: {elapsed:.1f}s  ({elapsed/60:.1f} min)")
    local_path = state.get("local_path", "")
    # 将 Windows 路径同时赋值给 project_path_win（后续 Agent 使用）
    summary = AIMessage(content=f"✅ 代码克隆完成。本地路径：{local_path}")
    return {
        "project_path_win": local_path,
        "messages": [summary],
        "next_agent": "scanner",
        "current_agent": "git",
        "git_status": "success",
        "timestamps": timestamps,
    }


# ==========================================
# 构建子图
# ==========================================
def create_git_agent() -> StateGraph:
    workflow = StateGraph(GitAgentState)

    workflow.add_node("precheck", precheck_node)
    workflow.add_node("agent", call_model)
    workflow.add_node("tools", tool_node)
    workflow.add_node("verify", verify_clone)
    workflow.add_node("fix", fix_node)
    workflow.add_node("extract", extract_result)

    workflow.set_entry_point("precheck")

    workflow.add_conditional_edges(
        "precheck",
        lambda s: "skip" if s.get("verify_ok") else ("agent" if s.get("precheck_ok") else "fix"),
        {"skip": "extract", "agent": "agent", "fix": "fix"}
    )

    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", "verify": "verify"}
    )

    workflow.add_edge("tools", "agent")

    workflow.add_conditional_edges(
        "verify",
        lambda s: "extract" if s.get("verify_ok") else "fix",
        {"extract": "extract", "fix": "fix"}
    )

    workflow.add_conditional_edges(
        "fix",
        lambda s: "agent" if s.get("fix_strategy") == "retry" else END,
        {"agent": "agent", END: END}
    )

    workflow.add_edge("extract", END)

    return workflow.compile()


git_agent_node = create_git_agent()