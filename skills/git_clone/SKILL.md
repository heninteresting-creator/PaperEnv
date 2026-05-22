name: git_clone
description: 在 Windows 环境下克隆 Git 仓库，自动多源加速和代理支持，返回本地绝对路径。
allowed-tools: skill_git_clone

triggers:
  - git clone
  - 克隆
  - 下载仓库
  - github.com
---

# Git 仓库克隆技能

## 适用场景
- 用户要求将远程 Git 仓库克隆到本地 Windows 目录。

## 可用 Skill 工具

### skill_git_clone
- **参数**：
  - `repo_url`: Git 仓库完整 URL（如 `https://github.com/user/repo.git`）
  - `target_root`: 本地目标根目录（Windows 路径，如 `D:\Projects`）
    - 默认值：由预检模块从 `base_cwd_win` 或当前工作目录自动获取
    - 仓库将被克隆到 `<target_root>\<repo_name>\`
  - `proxy_url` (可选): HTTP/HTTPS 代理地址，如 `http://127.0.0.1:7890`
    - 默认值：`""`（空字符串，表示不走代理）
    - 如果提供，**所有**克隆尝试（含加速源和直连）都将通过该代理进行
- **返回**：JSON 字符串，包含以下字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | bool | 克隆是否成功 |
| `local_path` | str | 克隆后的 Windows 绝对路径（成功时） |
| `used_proxy` | str | 使用的源别名：`"gh-proxy.org"`、`"gitdelivr.net"`、`"direct"` |
| `repo_name` | str | 仓库目录名 |
| `error` | str | 失败原因（失败时） |

## 标准工作流

本技能由 **Git Agent** 统一调度，分三个阶段执行：

### 1. 预检阶段（由 Git Agent 调用 `run_precheck`）
- 从用户消息中提取 `repo_url`、`target_root`、`proxy_url`
- 验证参数有效性（URL 格式、目录设置等）
- **注意**：Windows 环境下代理 `127.0.0.1` 可直接使用，无需修正

### 2. 执行阶段（调用 `skill_git_clone`）
- 传入预检后的参数
- 内部依次尝试多个加速源（`gh-proxy.org` → `gitdelivr.net` → 直连）
- 若提供了代理，所有请求均通过代理发出
- 返回最终结果（成功或已穷尽所有重试后的失败）

### 3. 验证阶段（由 Git Agent 调用 `verify_clone`）
- 在 Windows 本地检查目标路径下的 `.git` 目录是否存在
- 确认克隆有效性后，将 `local_path` 写入全局状态 `project_path_win`

## 错误处理（职责分层）

| 层级 | 职责 |
|------|------|
| **Skill** (`skill_git_clone`) | 多源重试（加速源1 → 加速源2 → 直连），返回最终成功/失败 |
| **Agent** (`fix` 节点) | 若 Skill 返回失败，根据错误类型决策：询问代理 / 重试 / 中止 |

**注意**：
- Skill 内部已完成全部重试，Agent 不应让 Skill 再次执行相同参数，除非用户提供了新的代理地址。
- 克隆成功后，`local_path` 将作为项目路径传递给下游 Agent（存入 `project_path_win`）。

## 注意事项
- 严禁自行拼接 `git clone` 命令，必须通过工具调用 `skill_git_clone`。
- 如果克隆失败且错误提示涉及网络，可询问用户是否提供代理地址。
- 所有路径使用 Windows 格式（如 `D:\Projects\repo`），由预检模块统一处理。