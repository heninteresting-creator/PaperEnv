import os
import subprocess
import signal
from langchain_core.tools import tool

current_process = None

def kill_process(proc: subprocess.Popen):
    if proc.poll() is not None:
        return
    try:
        if os.name == 'nt':
            subprocess.run(f"taskkill /F /T /PID {proc.pid}", shell=True, capture_output=True)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception:
        proc.kill()

@tool
def run_cli(command: str, timeout: int = 360) -> str:
    """在 Windows PowerShell 中执行命令，支持超时和中断。"""
    print(f"\n⚙️ [CLI] 执行命令: {command}")
    global current_process

    try:
        proc = subprocess.Popen(
            ["powershell", "-Command", command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='gbk', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            start_new_session=True if os.name != 'nt' else False
        )
        current_process = proc
        try:
            stdout, stderr = proc.communicate(timeout=timeout if timeout > 0 else None)
        except subprocess.TimeoutExpired:
            kill_process(proc)
            return f"命令执行超时（{timeout}秒），已强制终止。"
        finally:
            if current_process == proc:
                current_process = None
        output = stdout + stderr
        return output.strip() if output.strip() else "命令执行成功，但没有返回文字结果。"
    except Exception as e:
        return f"执行失败: {str(e)}"

@tool
def run_wsl(command: str, cwd: str = "", timeout: int = 0) -> str:
    """
    在 WSL Ubuntu 中执行命令。
    :param command: 要执行的 Linux 命令
    :param cwd: WSL 格式的绝对路径（如 /mnt/c/project），确保命令在该目录下执行
    :param timeout: 超时时间
    """
    # 核心修改：如果传入了 cwd，强制先 cd 过去再执行
    if cwd:
        # 使用 && 确保只有 cd 成功才执行后续命令
        command = f"cd {cwd} && {command}"
    
    print(f"\n⚙️ [WSL] 执行目录: {cwd if cwd else '默认'}")
    print(f"⚙️ [WSL] 执行命令: {command}")
    global current_process
    try:
        proc = subprocess.Popen(
            ["wsl", "bash", "-c", command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=False, start_new_session=True if os.name != 'nt' else False
        )
        current_process = proc
        def smart_decode(b_data: bytes) -> str:
            if not b_data:
                return ""
            try:
                return b_data.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    return b_data.decode('gbk')
                except UnicodeDecodeError:
                    return b_data.decode('utf-8', errors='replace')
        try:
            stdout, stderr = proc.communicate(timeout=timeout if timeout > 0 else None)
        except subprocess.TimeoutExpired:
            kill_process(proc)
            return f"WSL 命令执行超时（{timeout}秒），已强制终止。"
        finally:
            if current_process == proc:
                current_process = None
        out_str = smart_decode(stdout)
        err_str = smart_decode(stderr)
        if err_str and "command not found" in err_str.lower():
            return f"❌ 命令不存在：{err_str.strip()}"
        if err_str:
            return f"⚠️ 命令执行出错：\n{err_str.strip()}\n\n标准输出：\n{out_str.strip()}"
        if out_str.strip():
            return out_str.strip()
        return "命令执行成功，但没有返回文字结果。"
    except Exception as e:
        return f"WSL命令执行失败: {str(e)}"