name: docker
description: 在 Windows + WSL 环境下为 Python 项目配置 Docker 环境。包括创建 Dockerfile、禁用 BuildKit、构建镜像、运行容器、挂载当前目录。

allowed-tools: run_wsl, manage_file, web_search

triggers:
  - Docker
  - 容器
  - 镜像
  - 环境配置
  - docker build
  - docker run
  - 挂载
  - tail -f /dev/null
---

# Docker 环境配置技能

## 适用场景
- 用户要求在当前工作目录下构建 Docker 镜像并运行容器，用于项目代码的环境配置。
- 用户明确提到 **"Docker"、"容器"、"镜像"、"环境配置"、"docker build"、"docker run"** 等关键词。

## 环境假设
- 操作系统：Windows
- Shell：WSL Bash（Agent 的 `run_wsl` 工具默认使用）
- Docker Desktop 已安装，且 WSL2 后端正常工作

## 标准工作流（必须按顺序执行）

### 步骤 0：确保项目代码已存在于当前目录
- 如果用户提供了远程 Git 仓库，先激活 `git-clone` 技能将代码克隆到当前工作目录（WSL 路径）。
- 克隆完成后，使用 `cd <目录>` 进入该目录（后续所有命令均在此目录下执行）。

### 步骤 1：检查现有 Dockerfile

- 执行命令：`cat Dockerfile`（使用 `run_wsl`）
  
- 如果文件存在且内容正确（包含 `FROM python:3.11-slim`、`WORKDIR /app`、`CMD ["tail", "-f", "/dev/null"]`），则跳过步骤 2。
  
- 如果文件不存在或内容错误，继续步骤 2。
  
### 步骤 2：创建 Dockerfile（必须使用 `manage_file` 工具）
- **禁止**使用 `run_wsl echo ... > Dockerfile`（会导致编码错误和乱码）。
  
- 文件路径：`Dockerfile`
  
- 文件内容（完整复制）：
  ```dockerfile
  FROM python:3.11-slim
  WORKDIR /app
  COPY . /app
  ENV PYTHONUNBUFFERED=1
  CMD ["tail", "-f", "/dev/null"]
  ```

### 步骤 3：禁用 BuildKit（避免 INTERNAL_ERROR）
- 执行命令：export DOCKER_BUILDKIT=0
- 原因：默认 BuildKit 在某些环境下会报 stream terminated by RST_STREAM 错误。
- 注意：该设置仅对当前 WSL 会话有效。如需永久禁用，请建议用户添加系统环境变量 DOCKER_BUILDKIT=0。

### 步骤 4：构建镜像
- 命令：docker build -t <镜像名> .
- 镜像名默认使用当前目录名（小写，下划线替换为横线），或使用用户指定的名称。
- 示例：docker build -t my-python-projects .

### 步骤 5：删除可能存在的同名容器
- 命令：docker rm -f <容器名> 2>/dev/null
- 容器名建议为 <镜像名>-container（如 my-python-projects-container）。
- 2>/dev/null 用于忽略"容器不存在"的错误。

### 步骤 6：运行容器（后台，挂载当前目录）
- 命令：`docker run -d --name <容器名> -v <项目WSL绝对路径>:/app <镜像名>`
- **必须使用**从状态中获取的 WSL 绝对路径，例如 `-v /mnt/d/code/PaperEnv/samplemod:/app`。
- **严禁**使用 `$(pwd)`，因为每次 WSL 会话的工作目录不确定。

### 步骤 7：验证容器状态
- 命令：docker ps | grep <容器名>
- 预期输出：容器状态为 Up。

### 步骤 8（可选）：验证挂载内容
- 命令：docker exec <容器名> ls -la /app
- 预期输出：应该看到宿主机当前目录的所有文件。

## 常见问题与参考
当遇到以下具体错误时，请加载对应的补充文件（按需读取）：
|错误现象|参考文件|
|---|---|
|`echo >` 乱码、`${PWD}` 问题|`wsl-troubleshooting.md`|
|`INTERNAL_ERROR` 持续出现|`buildkit-fix.md`|
|容器立即退出|检查 `CMD` 是否为 `["tail", "-f", "/dev/null"]`|
|`Wsl/Service/0x8007274c`|`wsl-network-fix.md`（可选）|

如果上述补充文件不存在，则尝试以下通用方法：
- WSL 兼容性问题：确保所有路径使用 WSL 格式（/mnt/c/...）
- BuildKit 问题：确认已执行步骤 3
- 容器退出：使用 docker logs <容器名> 查看日志

## 最终输出要求
完成所有步骤后，向用户报告：
- 镜像名称、容器名称、容器 ID
- 容器运行状态（Up）
- 挂载目录信息
- 后续可以使用的命令示例（如 docker exec -it <容器名> bash）

## 注意事项
- 所有操作必须使用 run_wsl 工具（WSL Bash 环境），确保路径格式一致性。
- 不要执行无关的探测命令（如 dir、ls），除非步骤明确要求。
- 如果用户已提供 Dockerfile 且内容正确，不要覆盖或重复创建。
- 如果遇到步骤中未列出的错误，直接提示用户并提供错误信息，不要盲目重试。
- 所有路径必须使用 WSL 格式（/mnt/c/...），避免 Windows 路径转换问题。
- 确保 manage_file 工具使用 newline="\n"，防止换行符问题导致容器内执行失败。
- Docker 命令必须在 WSL 环境中执行，因为 Docker Desktop 的 WSL2 后端直接处理这些命令，避免路径转换问题。