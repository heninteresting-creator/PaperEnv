"""
多 Agent 流水线主程序
组装四个专职 Agent 子图，加入人工确认环节，启动交互式对话。
"""

import os
import re
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
import subprocess

from pipeline_state import PipelineState
from agents import (
    git_agent_node,
    scanner_agent_node,
    docker_builder_agent_node,
    installer_agent_node,
)

load_dotenv()


# ==========================================
# Reporter 节点（人工确认）
# ==========================================
def reporter_node(state: PipelineState) -> Command:
    """
    每个角色执行完毕后进入此节点，向用户汇报进度，等待用户指令。
    用户可以：
        - 'c' / 'continue' : 继续执行下一步
        - 'm: 修改要求'    : 带着修改指令回退到当前角色重新执行
        - 's' / 'stop'     : 终止任务
    """
    last_msg = state.get("messages", [])[-1] if state.get("messages") else None
    if last_msg:
        print(f"\n📢 [阶段完成] {last_msg.content}")
    else:
        print("\n📢 [阶段完成]")

    print("\n[等待确认] 请输入指令: 'c' 继续, 'm: 修改要求', 's' 停止")
    user_input = input("> ").strip()

    if user_input.lower() in ['s', 'stop']:
        print("🛑 用户终止任务。")
        return Command(graph=Command.INTERRUPT)

    elif user_input.lower().startswith('m:'):
        modify_instruction = user_input[2:].strip()
        print(f"✏️ 用户修改要求：{modify_instruction}")
        # 回退到当前角色，并注入修改指令
        current = state.get("current_agent", "git")
        return Command(
            goto=current,
            update={"messages": [HumanMessage(content=f"用户要求修改：{modify_instruction}")]}
        )

    else:
        # 默认继续到下一步
        next_agent = state.get("next_agent", "finish")
        
        # 防护：必须是图中实际存在的节点
        valid_nodes = {"git", "scanner", "docker_builder", "installer", "finish", "error_handler"}
        if next_agent not in valid_nodes:
            print(f"⚠️ [reporter] next_agent='{next_agent}' 不是有效节点，回退到 finish")
            next_agent = "finish"
        
        if next_agent == "finish":
            return Command(goto="finish")
        else:
            return Command(goto=next_agent)


def finish_node(state: PipelineState) -> dict:
    """流水线终点，输出完成报告。"""
    return {"messages": [AIMessage(content="🎉 全部任务完成！Docker 容器环境已就绪。")]}


# ==========================================
# 错误处理节点（可选）
# ==========================================
def error_handler_node(state: PipelineState) -> dict:
    """当流水线出现错误时，输出错误信息并终止。"""
    error_msg = state.get("error", "未知错误")
    return {"messages": [AIMessage(content=f"❌ 流水线发生错误：{error_msg}")]}


# ==========================================
# 路由器：根据 next_agent 决定跳转
# ==========================================
def router(state: PipelineState) -> str:
    """供 reporter 使用的条件边路由器"""
    next_agent = state.get("next_agent", "finish")
    if state.get("error"):
        return "error_handler"
    return next_agent


# ==========================================
# 构建主图
# ==========================================
def create_pipeline():
    workflow = StateGraph(PipelineState)

    # 添加四个角色子图（作为节点）
    workflow.add_node("git", git_agent_node)
    workflow.add_node("scanner", scanner_agent_node)
    workflow.add_node("docker_builder", docker_builder_agent_node)
    workflow.add_node("installer", installer_agent_node)

    # 添加辅助节点
    workflow.add_node("reporter", reporter_node)
    workflow.add_node("finish", finish_node)
    workflow.add_node("error_handler", error_handler_node)

    # 设置入口：默认从 Git 专员开始
    workflow.set_entry_point("git")

    # 每个角色执行完都进入 reporter
    workflow.add_edge("git", "reporter")
    workflow.add_edge("scanner", "reporter")
    workflow.add_edge("docker_builder", "reporter")
    workflow.add_edge("installer", "reporter")

    workflow.add_conditional_edges(
        "reporter",
        router,
        {
            "git": "git",
            "scanner": "scanner",
            "docker_builder": "docker_builder",
            "installer": "installer",
            "runner": "finish",        # ← Installer 成功/部分成功 → 先到 finish（Runner 未实现）
            "supervisor": "finish",    # ← Installer 核心包失败 → 先到 finish
            "finish": "finish",
            "error_handler": "error_handler"
        }
    )

    # finish 和 error_handler 直接结束
    workflow.add_edge("finish", END)
    workflow.add_edge("error_handler", END)

    # 使用内存检查点保存状态（支持中断恢复）
    memory = MemorySaver()
    return workflow.compile(checkpointer=memory)


# ==========================================
# 主交互循环
# ==========================================

def get_wsl_path(win_path: str) -> str:
    """将 Windows 路径转为标准 WSL /mnt/ 路径（驱动器号映射）"""
    import os
    drive, tail = os.path.splitdrive(win_path)
    if drive:
        drive_letter = drive[0].lower()
        wsl_path = f"/mnt/{drive_letter}{tail.replace('\\', '/')}"
        return wsl_path
    # 无盘符时（如网络路径），返回默认工作区
    return "/mnt/c/AgentWorkspace"

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 多 Agent 流水线已启动！(模型: Kimi-K2.6)")
    print("💡 提示: 输入 Git 仓库地址开始，或输入 'exit' 退出。")
    print("=" * 60)
    
    # 检查 API Key
    if not (os.getenv("SILICONFLOW_API_KEY") or os.getenv("OPENAI_API_KEY")):
        raise RuntimeError("未检测到 API Key。请在 .env 文件中设置 SILICONFLOW_API_KEY 或 OPENAI_API_KEY。")

    app = create_pipeline()
    config = {"configurable": {"thread_id": "session_1"}}


    while True:
        user_input = input("\n[You] > ").strip()
        if user_input.lower() in ['exit', 'quit', 'q']:
            print("再见！")
            break
        if not user_input:
            continue

        # 提取仓库 URL（简单正则匹配）
        url_match = re.search(r"(https?://[^\s]+\.git)", user_input)
        repo_url = url_match.group(1) if url_match else user_input  # 若未匹配到，则将整句当作 URL 尝试

        current_win_dir = os.getcwd()
        current_wsl_dir = get_wsl_path(current_win_dir)
    
        initial_state = {
        "messages": [HumanMessage(content=user_input)],
        "repo_url": repo_url,
        # 新增全局锚点：所有 Agent 必须在这个基准目录下干活
        "base_cwd_wsl": current_wsl_dir 
        }

        try:
            # 使用 stream_mode="updates" 观察每个节点的输出
            for output in app.stream(initial_state, config=config, stream_mode="updates"):
                for node_name, state_update in output.items():
                    if node_name == "reporter":
                        # reporter 的输出已在控制台交互中体现，无需额外打印
                        pass
                    elif node_name in ["finish", "error_handler"]:
                        # 终点节点直接打印最终消息
                        msgs = state_update.get("messages", [])
                        if msgs:
                            print(f"\n🤖 [系统] {msgs[-1].content}")
                    elif "messages" in state_update:
                        # 其他节点若返回了 messages，打印最新一条 AI 消息（用于阶段提示）
                        msgs = state_update["messages"]
                        if msgs and isinstance(msgs[-1], AIMessage) and msgs[-1].content:
                            print(f"\n🤖 [{node_name}] {msgs[-1].content}")
        except Exception as e:
            if "INTERRUPT" in str(e):
                print("任务已被用户终止。")
                break
            else:
                raise