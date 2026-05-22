"""
Git Clone 预检模块（Windows 版）
"""
import re
import os
from langchain_core.messages import HumanMessage

def run_precheck(state: dict) -> dict:
    issues = []
    updates = {}

    # 优先用 state 里已有的显式字段
    repo_url = (state.get("repo_url") or "").strip()
    target_root = (state.get("target_root") or "").strip()
    proxy = (state.get("git_proxy") or "").strip()

    # 只有当显式字段缺失时，才从 messages 兜底提取
    if not repo_url or not target_root:
        messages = state.get("messages", [])
        
        # 只从最新的 HumanMessage 提取
        content = ""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                content = msg.content if hasattr(msg, "content") else str(msg)
                break
        
        if not content and messages:
            msg = messages[-1]
            content = msg.content if hasattr(msg, "content") else str(msg)

        # 提取 repo_url：严格限定代码托管域名，防止匹配代理地址
        if not repo_url:
            url_match = re.search(
                r"https?://(?:github\.com|gitlab\.com|gitee\.com)/[^\s/]+/[^\s/]+(?:\.git)?",
                content
            )
            if url_match:
                repo_url = url_match.group(0).strip()
            else:
                issues.append("repo_url 无效或缺失")

        # 提取代理（独立正则，不会和 repo_url 混淆）
        if not proxy:
            proxy_match = re.search(
                r"(?:代理|proxy)\s*[:=]?\s*(https?://\S+:\d+)",
                content, re.IGNORECASE
            )
            if proxy_match:
                proxy = proxy_match.group(1).strip()

        if not target_root:
            target_root = state.get("base_cwd_win", os.getcwd())

    updates["repo_url"] = repo_url
    updates["target_root"] = target_root
    updates["git_proxy"] = proxy

    # 校验
    if not repo_url or not re.match(r"https?://(?:github|gitlab|gitee)\.com/[^\s/]+/[^\s/]+", repo_url):
        if "repo_url 无效或缺失" not in issues:
            issues.append("repo_url 无效或缺失")
    if not target_root:
        issues.append("target_root 未设置")

    real_issues = [i for i in issues if "修正" not in i]
    ok = len(real_issues) == 0

    return {
        "precheck_ok": ok,
        "issues": issues,
        "updates": updates
    }