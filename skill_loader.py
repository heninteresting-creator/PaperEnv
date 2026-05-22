import os
import importlib

SKILLS_DIR = "skills"

def load_skill_content(skill_name: str) -> str:
    """加载指定技能的 SKILL.md 内容"""
    skill_path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    if os.path.exists(skill_path):
        with open(skill_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def get_skill_system_prompt(skill_name: str, extra_context: str = "") -> str:
    """构建角色的系统提示"""
    skill_content = load_skill_content(skill_name)
    if not skill_content:
        return "你是一个智能助手，请根据用户需求完成任务。"
    
    base_prompt = f"""你是{skill_name}专家。必须严格遵循以下技能工作流，不得偏离步骤：

{skill_content}

{extra_context}
"""
    return base_prompt


def get_skill_functions(skill_name: str):
    """
    从 skills/<skill_name>/functions.py 中加载 @tool 装饰的函数列表。
    假设模块中定义了 TOOLS 变量（list of tools）。
    """
    # 将 skill_name 中的连字符转为下划线，确保合法 Python 模块名
    module_name = f"skills.{skill_name.replace('-', '_')}.functions"
    try:
        module = importlib.import_module(module_name)
        if hasattr(module, "TOOLS"):
            return module.TOOLS
        else:
            print(f"⚠️ 技能 {skill_name} 的 functions.py 缺少 TOOLS 变量")
            return []
    except ImportError as e:
        print(f"⚠️ 无法导入技能 {skill_name} 的函数模块: {e}")
        return []