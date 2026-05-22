# Agent Skills 导航索引

## 可用技能列表

| 技能名称 | 触发关键词 | 简要描述 |
|---------|-----------|----------|
| `git-clone` | git clone, 克隆, 网络超时, 代理 | 处理 git clone 超时问题，包括代理配置和宿主机降级方案 |
| `docker` | Docker, 容器, 镜像, 环境配置, docker build, docker run | 在 wsl 环境下构建 Docker 镜像、运行容器、挂载目录 |
| `requirements` | 依赖分析, 扫描依赖, 生成依赖文件, requirements.txt, 第三方包 | 智能扫描 Python 项目依赖，审查优化原始导入列表，为生成 requirements.txt 做准备 |
| `python-deps` | pip install, requirements, 依赖安装, 环境依赖 | 在容器内安装 Python 项目依赖，处理镜像源和网络问题 |

## 技能选择规则

1. **优先匹配**：根据用户输入中的关键词匹配上述技能名称
2. **多技能组合**：如果用户需求涉及多个领域，依次激活相关技能
3. **技能降级**：如果特定技能不存在，回退到通用技能或提示用户
