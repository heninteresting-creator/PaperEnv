from typing import Annotated, Optional, TypedDict, Dict, Any, List
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage

class PipelineState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    next_agent: Optional[str]
    current_agent: Optional[str]
    timestamps: Optional[Dict[str, Any]]      # ← 新增：各阶段时间戳
    
    # Git 阶段
    repo_url: Optional[str]
    project_path_wsl: Optional[str]
    project_path_win: Optional[str]
    git_proxy: Optional[str]
    proxy_corrected: Optional[bool]
    git_status: Optional[str]
    local_path: Optional[str]
    
    # Scanner 阶段
    scan_phase: Optional[str]
    scan_imports_result: Optional[Dict[str, Any]]
    resolve_result: Optional[Dict[str, Any]]
    requirements_path: Optional[str]
    facts: Optional[Dict[str, Any]]
    structure_analysis: Optional[Dict[str, Any]]
    framework: Optional[str]
    python_version: Optional[str]
    system_deps: Optional[List[str]]
    risk_packages: Optional[List[str]]
    dependencies: Optional[List[str]]
    heavy_deps: Optional[List[Dict[str, Any]]]
    needs_project_build: Optional[bool]      # ← 新增
    pytorch_cuda_version: Optional[str]      # ← 新增
    
    # Docker Builder 阶段
    build_phase: Optional[str]
    docker_image: Optional[str]
    container_name: Optional[str]
    container_id: Optional[str]
    docker_status: Optional[str]
    dockerfile_path: Optional[str]
    dockerfile_content: Optional[str]
    build_log: Optional[str]
    docker_error: Optional[str]
    
    # Installer 阶段
    install_phase: Optional[str]
    install_method: Optional[str]
    install_log: Optional[str]
    failed_packages: Optional[List[str]]
    skipped_packages: Optional[List[str]]
    install_status: Optional[str]
    install_retry_count: Optional[int]
    
    # 流程控制
    error: Optional[str]
    retry_count: Optional[int]
    fix_strategy: Optional[str]
    verify_ok: Optional[bool]
    precheck_ok: Optional[bool]