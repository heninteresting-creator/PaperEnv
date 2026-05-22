name: installer
description: 在运行中的 Docker 容器内安装 Python 依赖，并验证导入。纯 uv 路线，不混合 conda/pip。
triggers:
  - 安装依赖
  - pip install
  - requirements
  - 装包
  - 环境配置
---

# Installer 技能

## 适用场景
- Docker 容器已启动且代码已挂载。
- 需要安装 `requirements_agent.txt` 中的依赖并验证可导入。
- 用户提到 "安装依赖"、"pip install"、"requirements"、"装包"。

## 环境假设
- 容器镜像：由 Docker Builder 生成的 `dyfo-env:latest`（基于 nvidia/cuda + uv）
- 容器名：默认 `dyfo-container`
- 代码挂载点：`-v project_path:/app`，容器内路径固定为 `/app`
- 包管理器：**只认 uv**，不检查 mamba/conda/pip

## 标准工作流（必须按顺序执行）

### 步骤 0：预检（AgentMonitor）
1. 容器状态 `docker inspect -f '{{.State.Status}}' container_name` 必须为 `running`。
2. 容器内 `/workspace/requirements_agent.txt` 存在。
3. 容器内 `which uv` 必须返回成功。
4. 网络检查用 `python -c "import urllib.request; urllib.request.urlopen('https://pypi.org', timeout=5)"`。
5. **空文件处理**：若文件存在但过滤注释后无实质包名，标记 `skip_install=True`，直接跳到步骤 3（验证空列表，自然全过）。

**预检失败直接 abort，不进安装。**

### 步骤 1：安装（单层 uv，不内部换源）
- **默认读取** `/workspace/requirements_agent.txt`（Scanner 2.1 最终输出，已剔除 heavy 包）。
- **Installer 内部剔包重试时**：生成临时文件 `/workspace/requirements_filtered.txt`（由 `current_requirements_path` 状态字段跟踪），移除安装失败的包后重试。
- `requirements_filtered.txt` 是 Installer 内部临时文件，不是 Scanner 的输出。
- 命令：`uv pip install -r &lt;current_requirements_path&gt;`
- 如果 `extra_index_url` 存在（如 torch CUDA 版），追加 `--extra-index-url`。
- 如果 `current_index_url` 不是默认 PyPI，追加 `--index-url`。
- **skill 只做单层安装**，失败时返回 `failed_packages` 和完整 stderr，**不内部换源重试**。

### 步骤 2：修复（AgentFix）
安装失败时，Agent 状态机按以下顺序修复：
1. **换源重试**：官方 PyPI → 清华 → 阿里（`current_index_url` 状态驱动）。
2. **剔包重试**：解析失败包名，生成 `/workspace/requirements_filtered.txt`，移除失败包后重试。
3. **abort**：重试 3 次仍失败，或所有包均失败，则终止 Installer。

### 步骤 3：验证（Checkpoint）
- 读取**当前实际使用的** requirements 文件（可能是剔包后的 `/workspace/requirements_filtered.txt`），实时解析包列表。
- 对每个包执行 `python -c "import xxx; print(xxx.__version__)"`。
- **模块名映射**（必须遵守）：
  - `opencv-python-headless` → `cv2`
  - `pillow` → `PIL`
  - `scikit-learn` → `sklearn`
  - `scikit-image` → `skimage`
  - `hydra-core` → `hydra`
  - `pyyaml` → `yaml`
  - `python-dateutil` → `dateutil`
  - `attrs` → `attrs`
- 版本获取双层 fallback：先 `__version__`，失败用 `importlib.metadata.version()`。

### 步骤 4：出口
- **核心包失败（torch / vllm / transformers / numpy）** → `install_status="failed"`，路由到 `supervisor`，不继续到 Runner。
- **非核心包失败** → `install_status="partial"`，路由到 `runner`，Runner 阶段可能 ImportError。
- **全部通过** → `install_status="success"`，路由到 `runner`。

## 注意事项
- 所有路径使用容器内绝对路径 `/workspace/...`，不使用 Windows 路径。
- `requirements_agent.txt` 由 Scanner Agent 生成，包名已做下划线→横线转换。
- uv 命令必须在容器内通过 `docker exec` 执行，不要在宿主机直接运行。
- 安装日志保留 stderr 前 2000 字符，供上层解析失败包。
- 空 requirements 文件不报错，直接跳过安装并标记 `skip_install=True`。