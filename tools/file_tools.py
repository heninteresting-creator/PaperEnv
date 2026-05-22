import os
from pathlib import Path
from typing import Optional
from langchain_core.tools import tool

def convert_wsl_to_windows(path_str: str) -> str:
    """
    将 WSL 格式路径 (/mnt/d/...) 转换为 Windows 格式 (d:/...)
    如果已经是 Windows 格式或非 WSL 路径，则原样返回。
    """
    if not isinstance(path_str, str):
        path_str = str(path_str)
        
    if os.name == 'nt' and path_str.startswith('/mnt/'):
        parts = path_str.split('/')
        if len(parts) >= 3:
            drive = parts[2]  # 获取盘符，例如 'd'
            rest = '/'.join(parts[3:])
            return f"{drive}:/{rest}"
    return path_str

@tool
def manage_file(operation: str, file_path: str, content: str = "") -> str:
    """
    对文件进行读、写、删除操作。
    
    Args:
        operation: 动作名称，可选 'write', 'read', 'delete'
        file_path: 目标文件路径 (支持 WSL 路径格式)
        content: 写入内容 (仅在 write 操作时生效)
    """
    # 1. 路径转换 (解决跨系统访问问题)
    real_path_str = convert_wsl_to_windows(file_path)
    target_path = Path(real_path_str)
    
    print(f"📁 [文件工具] 正在执行 {operation} -> {real_path_str}")

    try:
        if operation == "write":
            # 确保父目录存在
            target_path.parent.mkdir(parents=True, exist_ok=True)
            # 写入文件
            target_path.write_text(content, encoding="utf-8")
            return f"✅ 成功写入文件: {real_path_str}"

        elif operation == "read":
            if target_path.exists():
                return target_path.read_text(encoding="utf-8")
            else:
                return f"❌ 错误：文件不存在 -> {real_path_str}"

        elif operation == "delete":
            if target_path.exists():
                target_path.unlink()
                return f"✅ 成功删除文件: {real_path_str}"
            else:
                return f"❌ 错误：文件不存在，无法删除 -> {real_path_str}"

        else:
            return f"❌ 错误：不支持的操作类型 '{operation}'"

    except Exception as e:
        return f"❌ 文件操作失败，原因: {str(e)}"

@tool
def read_file_chunk(file_path: str, chunk_size: int = 1000) -> str:
    """读取文件的起始部分内容。"""
    real_path = convert_wsl_to_windows(file_path)
    try:
        with open(real_path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read(chunk_size)
    except Exception as e:
        return f"读取失败: {str(e)}"