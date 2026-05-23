"""
扫描员子图 (Subgraph) —— Scanner 2.1（DEBUG 版）
"""

import os
import re
import json
import time
import traceback
from pathlib import Path
from typing import Optional, Dict, Any, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langgraph.graph import StateGraph, END

from pipeline_state import PipelineState
from skill_loader import get_skill_system_prompt

# 动态加载 scanner functions
import importlib
scanner_functions = importlib.import_module("skills.scan_import.functions")
collect_facts = scanner_functions.collect_facts
extract_dependencies = scanner_functions.extract_dependencies
get_fallback_analysis = scanner_functions.get_fallback_analysis
verify_llm_analysis = scanner_functions.verify_llm_analysis

# 加载 web_search Tool
from tools.web_search import web_search


# ==========================================
# 重依赖包识别表
# ==========================================
HEAVY_PACKAGES: Dict[str, Dict[str, Any]] = {
    "horovod": {
        "category": "compile_with_system_deps",
        "reason": "需 CMake + OpenMPI + PyTorch 头文件编译",
        "system_deps": ["libopenmpi-dev", "openmpi-bin"],
        "python_pre": ["torch", "packaging"],
        "needs_cuda": True,
        "fix_hint": "apt-get update && apt-get install -y libopenmpi-dev openmpi-bin && uv pip install torch packaging --system && HOROVOD_WITH_PYTORCH=1 uv pip install horovod --system --no-build-isolation",
    },
    "flash-attn": {
        "category": "compile_with_system_deps",
        "reason": "需 CUDA 编译",
        "needs_cuda": True,
        "fix_hint": "需确认容器已配 CUDA Toolkit，然后 uv pip install flash-attn --system --no-build-isolation",
    },
    "apex": {
        "category": "compile_with_system_deps",
        "reason": "需 CUDA 编译",
        "needs_cuda": True,
        "fix_hint": "git clone https://github.com/NVIDIA/apex && cd apex && uv pip install -v --disable-pip-version-check --no-build-isolation --system ./",
    },
    "sam2": {
        "category": "git_install",
        "reason": "未发布 PyPI，需 git 安装",
        "fix_hint": "手动 git clone https://github.com/facebookresearch/segment-anything-2.git 并 pip install -e .",
    },
    "lang-sam": {
        "category": "unresolvable",
        "reason": "包名/源异常，uv 无法解析",
        "fix_hint": "查看项目 GitHub 仓库的手动安装说明",
    },
}


def _is_heavy_package(pypi_name: str) -> Optional[Dict[str, Any]]:
    bare = pypi_name.split("[")[0].strip()
    for sep in ["==", ">=", "<=", "!=", ">", "<", "@"]:
        if sep in bare:
            bare = bare.split(sep)[0].strip()
    bare = bare.lower().replace("_", "-").replace(".", "-")
    aliases = {"sam-2": "sam2", "sam_2": "sam2", "lang_sam": "lang-sam"}
    bare = aliases.get(bare, bare)
    return HEAVY_PACKAGES.get(bare)


def _split_heavy_packages(deps: List[str]) -> tuple[List[str], List[Dict[str, Any]]]:
    normal, heavy = [], []
    for pkg in deps:
        meta = _is_heavy_package(pkg)
        if meta:
            bare = pkg.split("[")[0].strip()
            for sep in ["==", ">=", "<=", "!=", ">", "<", "@"]:
                if sep in bare:
                    bare = bare.split(sep)[0].strip()
            bare = bare.lower().replace("_", "-").replace(".", "-")
            heavy.append({"name": pkg, "bare": bare, "meta": meta})
            print(f"   ⚠️ 识别到重依赖包（将跳过自动安装）: {pkg}")
        else:
            normal.append(pkg)
    return normal, heavy


# ---------- 状态定义 ----------
class ScannerAgentState(PipelineState, total=False):
    scan_phase: Optional[str]
    facts: Optional[Dict[str, Any]]
    structure_analysis: Optional[Dict[str, Any]]
    identified_packages: Optional[List[Dict[str, Any]]]
    verification_result: Optional[Dict[str, Any]]
    fusion_notes: Optional[str]
    retry_count: Optional[int]
    verify_ok: Optional[bool]
    error: Optional[str]
    requirements_path: Optional[str]
    fix_strategy: Optional[str]
    precheck_ok: Optional[bool]
    framework: Optional[str]
    python_version: Optional[str]
    system_deps: Optional[List[str]]
    risk_packages: Optional[List[str]]
    dependencies: Optional[List[str]]
    heavy_deps: Optional[List[Dict[str, Any]]]


# ---------- LLM 实例 ----------
_llm_raw = None

def _get_llm_raw():
    global _llm_raw
    if _llm_raw is None:
        print(f"   [DEBUG] 初始化 LLM: model=Pro/moonshotai/Kimi-K2.6, base=https://api.siliconflow.cn/v1")
        _llm_raw = ChatOpenAI(
            model="Pro/moonshotai/Kimi-K2.6",
            openai_api_key=(os.getenv("SILICONFLOW_API_KEY") or os.getenv("OPENAI_API_KEY") or ""),
            openai_api_base="https://api.siliconflow.cn/v1",
            temperature=0
        )
    return _llm_raw


# ==========================================
# 节点 0：预检 + 事实收集
# ==========================================
def precheck_node(state: ScannerAgentState) -> dict:
    print(f"\n{'='*60}")
    print(f"[DEBUG] >>> 进入 precheck_node")
    project_path = state.get("project_path_win", "").strip()
    timestamps = state.get("timestamps", {})
    if not timestamps.get("pipeline_start"):
        timestamps["pipeline_start"] = time.time()
    timestamps["scanner_start"] = time.time()
    print(f"[DEBUG] project_path_win = {project_path!r}")
    
    if not project_path:
        print(f"[DEBUG] ❌ project_path_win 为空")
        return {
            "precheck_ok": False,
            "error": "未获取到项目路径 (project_path_win 为空)",
            "scan_phase": "error"
        }
    
    if not os.path.exists(project_path):
        print(f"[DEBUG] ❌ 路径不存在: {project_path}")
        return {
            "precheck_ok": False,
            "error": f"项目路径不存在: {project_path}",
            "scan_phase": "error"
        }
    
    if not os.path.isdir(project_path):
        print(f"[DEBUG] ❌ 路径不是目录: {project_path}")
        return {
            "precheck_ok": False,
            "error": f"项目路径不是目录: {project_path}",
            "scan_phase": "error"
        }
    
    print(f"[DEBUG] ✅ 路径有效，开始 collect_facts...")
    facts = collect_facts(project_path)
    ft_lines = facts.get("file_tree", "").split("\n")
    print(f"[DEBUG] collect_facts 完成:")
    print(f"   - 文件数: {len(ft_lines)}")
    print(f"   - 依赖文件: {facts.get('detected_dependency_files', [])}")
    print(f"   - imports: {len(facts.get('imports', []))}")
    print(f"   - internal_modules: {len(facts.get('internal_modules', []))}")

    # === 覆盖三类：检测项目本身是否需要编译安装 ===
    needs_project_build = False
    setup_py = os.path.join(project_path, "setup.py")
    pyproject = os.path.join(project_path, "pyproject.toml")
    for f_path in [setup_py, pyproject]:
        if os.path.exists(f_path):
            try:
                with open(f_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                compile_markers = ['CUDAExtension', 'cpp_extension', 'CUDABackend', 
                                   'ext_modules', 'BuildExtension', 'torch.utils.cpp_extension']
                if any(m in content for m in compile_markers):
                    needs_project_build = True
                    print(f"[DEBUG] 检测到项目含编译扩展: {os.path.basename(f_path)}")
                    break
            except Exception:
                pass
    facts["needs_project_build"] = needs_project_build
    
    result = {
        "precheck_ok": True,
        "project_path_win": project_path,
        "scan_phase": "identifying",
        "facts": facts,
        "retry_count": 0,
        "error": "",
        "verify_ok": False,
        "timestamps": timestamps,
    }
    print(f"[DEBUG] <<< 退出 precheck_node, scan_phase={result['scan_phase']}")
    return result


# ==========================================
# 节点 1：LLM 识别（identifying）
# ==========================================
def call_model(state: ScannerAgentState) -> dict:
    print(f"\n{'='*60}")
    print(f"[DEBUG] >>> 进入 call_model")
    
    phase = state.get("scan_phase", "identifying")
    facts = state.get("facts", {})
    project_path = state.get("project_path_win", "")
    print(f"[DEBUG] phase={phase}, project_path={project_path}")
    
    if phase != "identifying":
        print(f"[DEBUG] ⚠️ phase 不是 identifying，返回异常")
        return {"messages": [AIMessage(content="阶段异常")]}
    
    # 构造 prompt
    file_tree = facts.get("file_tree", "")[:1500]
    dep_snippets = ""
    for path, content in list(facts.get("dependency_file_snippets", {}).items())[:3]:
        dep_snippets += f"\n【{path} (前50行)】\n{content[:800]}\n"
    
    imports_str = ", ".join(facts.get("imports", [])[:50])
    notebook_imports = ", ".join(facts.get("notebook_imports", [])[:20])
    readme = facts.get("readme_snippet", "")[:500]
    setup_failed = "是" if facts.get("setup_py_parse_failed") else "否"
    setup_deps = ", ".join(facts.get("setup_py_deps", []))
    internal_modules_str = ", ".join(facts.get("internal_modules", [])[:30])
    file_packages_str = ", ".join(facts.get("file_packages", [])[:50])
    
    example_json = json.dumps({
        "dependency_files": [
            {"path": "requirements.txt", "priority": 1, "reason": "主依赖声明"}
        ],
        "framework": "pytorch",
        "python_version": "3.10",
        "system_deps": ["cmake"],
        "identified_packages": [
            {"import_name": "open_clip", "pypi_name": "open-clip-torch", "category": "third_party", "reason": "OpenCLIP 官方 PyPI 名", "confidence": "high"},
            {"import_name": "cv2", "pypi_name": "opencv-python", "category": "third_party", "reason": "OpenCV 的 Python 绑定", "confidence": "high"},
            {"import_name": "utils", "category": "internal", "reason": "项目内部目录 utils/ 存在", "confidence": "high"}
        ],
        "notes": ""
    }, ensure_ascii=False, indent=2)
    
    system_prompt = f"""你是一位 Python 软件包识别专家...

【项目文件树（前3层）】
{file_tree}

【已发现的依赖文件内容】{dep_snippets}

【静态提取的 import 列表（前50个）】
{imports_str}

【Notebook 中提取的依赖】
{notebook_imports}

【README 相关段落】
{readme}

【setup.py AST 提取结果】
解析成功: {setup_failed}
提取到的依赖: {setup_deps}

【内部模块参考】
{internal_modules_str}

【依赖文件中的包名】
以下包名来自 requirements.txt / pyproject.toml / setup.py 等依赖声明文件，请与 import 列表合并统一判断：
{file_packages_str}

【合并规则】
1. import 名和文件包名指向同一个 PyPI 包的，只保留标准名（如 sam-2 和 sam2 统一为 sam2）。
2. 文件中出现了 import 列表里没有的包，请判断：
   - 如果是运行时/编译依赖（如 uvicorn, ninja, einops, omegaconf），保留并标注 source: "file"
   - 如果是开发/调试工具（如 debugpy, pytest），排除
3. 以下包排除（开发/调试工具）：debugpy, ipykernel, ipython, jupyter, notebook, nbformat, pytest, black, flake8, mypy, isort
4. OpenCV 变体只保留 opencv-python-headless（如果存在）。

要求：
1. 对每个 import，回忆它是否是知名开源项目，给出官方 PyPI 名。
2. 不要只做下划线变横线的字面转换！
3. 判断为内部模块的，category 写 "internal"；第三方的写 "third_party"。
4. 必须填写 reason 字段。
5. 严格输出 JSON，不要 markdown 代码块，不要分析文字。

JSON 示例：
{example_json}"""

    print(f"[DEBUG] system_prompt 长度: {len(system_prompt)} chars")
    print(f"[DEBUG] 准备调用 LLM...")
    
    dialogs = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"分析项目 {project_path} 的结构并输出 JSON")
    ]
    
    try:
        llm = _get_llm_raw()
        print(f"[DEBUG] LLM 实例获取成功，开始 invoke...")
        response = llm.invoke(dialogs)
        print(f"[DEBUG] ✅ LLM invoke 成功")
        print(f"[DEBUG] response type: {type(response)}")
        print(f"[DEBUG] response.content 长度: {len(response.content or '')}")
        print(f"[DEBUG] response.content 前200: {response.content[:200]!r}")
        
        result = {"messages": [response]}
        print(f"[DEBUG] <<< 退出 call_model, 返回 messages 数量: {len(result['messages'])}")
        return result
        
    except Exception as e:
        print(f"[DEBUG] ❌ LLM invoke 异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        # 返回一个带错误标记的 AIMessage，让下游能进入 fix
        error_msg = AIMessage(content=f'{{"error": "LLM调用失败: {str(e)}"}}')
        return {"messages": [error_msg]}


# ==========================================
# 节点 2：Schema 校验
# ==========================================
def verify_schema_node(state: ScannerAgentState) -> dict:
    print(f"\n{'='*60}")
    print(f"[DEBUG] >>> 进入 verify_schema_node")
    
    facts = state.get("facts", {})
    messages = state.get("messages", [])
    print(f"[DEBUG] state messages 数量: {len(messages)}")
    
    # 找到最后一个 AIMessage
    ai_msg = None
    for i, msg in enumerate(reversed(messages)):
        if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
            ai_msg = msg
            print(f"[DEBUG] 找到 AIMessage 在倒数第 {i+1} 位, content长度={len(msg.content or '')}")
            break
    
    if not ai_msg:
        print(f"[DEBUG] ❌ 未找到 AIMessage")
        return {
            "verify_ok": False,
            "error": "未检测到 LLM 分析输出",
            "scan_phase": "identifying"
        }
    
    print(f"[DEBUG] 开始 verify_llm_analysis...")
    ok, analysis, error = verify_llm_analysis(ai_msg.content, facts)
    print(f"[DEBUG] verify_llm_analysis 结果: ok={ok}, error={error!r}")
    
    if not ok:
        print(f"[DEBUG] ❌ 解析失败: {error}")
        return {
            "verify_ok": False,
            "error": f"LLM 结构分析解析失败: {error}",
            "scan_phase": "identifying",
        }
    
    identified = analysis.get("identified_packages", [])
    print(f"[DEBUG] identified_packages 数量: {len(identified)}")
    if identified:
        print(f"[DEBUG] 前3个: {identified[:3]}")
    
    # 格式校验
    if not isinstance(identified, list):
        print(f"[DEBUG] ❌ identified_packages 不是列表: {type(identified)}")
        return {
            "verify_ok": False,
            "error": f"identified_packages 不是列表: {type(identified)}",
            "scan_phase": "identifying",
        }
    
    valid_identified = []
    for pkg in identified:
        if isinstance(pkg, dict) and "import_name" in pkg and "category" in pkg:
            valid_identified.append(pkg)
    
    print(f"[DEBUG] 有效 identified_packages: {len(valid_identified)} / {len(identified)}")
    
    if not valid_identified and facts.get("imports"):
        print(f"[DEBUG] ❌ 有效包为空但项目有 imports")
        return {
            "verify_ok": False,
            "error": "identified_packages 为空或格式异常",
            "scan_phase": "identifying",
        }
    
    result = {
        "verify_ok": True,
        "scan_phase": "extracting",
        "structure_analysis": analysis,
        "identified_packages": valid_identified,
        "error": "",
    }
    print(f"[DEBUG] <<< 退出 verify_schema_node, verify_ok=True")
    return result


# ==========================================
# Fusion 内联工具
# ==========================================
PYPI_NAME_MAP = {
    "cv2": "opencv-python", "PIL": "Pillow", "sklearn": "scikit-learn",
    "skimage": "scikit-image", "yaml": "PyYAML", "dotenv": "python-dotenv",
    "jwt": "PyJWT", "Image": "Pillow", "pil": "Pillow", "bs4": "beautifulsoup4",
    "Crypto": "pycryptodome", "MySQLdb": "mysqlclient", "pymysql": "PyMySQL",
    "flask": "Flask", "django": "Django", "fastapi": "fastapi",
    "sqlalchemy": "SQLAlchemy", "pandas": "pandas", "numpy": "numpy",
    "matplotlib": "matplotlib", "scipy": "scipy", "tensorflow": "tensorflow",
    "torch": "torch", "transformers": "transformers", "requests": "requests",
    "urllib3": "urllib3", "click": "click", "jinja2": "Jinja2",
    "markdown": "Markdown", "psycopg2": "psycopg2-binary", "redis": "redis",
    "celery": "celery", "pytest": "pytest", "sphinx": "Sphinx",
    "twisted": "Twisted", "lxml": "lxml", "html5lib": "html5lib",
    "openpyxl": "openpyxl", "xlrd": "xlrd", "xlsxwriter": "XlsxWriter",
    "googleapiclient": "google-api-python-client",
}


def _apply_mapping_fallback(identified_packages: List[Dict]) -> List[Dict]:
    for pkg in identified_packages:
        imp = pkg.get("import_name", "")
        if pkg.get("category") == "third_party" and imp in PYPI_NAME_MAP:
            correct = PYPI_NAME_MAP[imp]
            if pkg.get("pypi_name") != correct:
                pkg["pypi_name"] = correct
                pkg["reason"] = pkg.get("reason", "") + f" [映射表兜底: {imp}→{correct}]"
                pkg["confidence"] = "high"
        elif pkg.get("category") == "internal" and imp in PYPI_NAME_MAP:
            pkg["category"] = "third_party"
            pkg["pypi_name"] = PYPI_NAME_MAP[imp]
            pkg["reason"] = pkg.get("reason", "") + f" [反修正: LLM误判为内部，映射表确认是 {PYPI_NAME_MAP[imp]}]"
    return identified_packages


def _safe_search(import_name: str, timeout: int = 5) -> str:
    try:
        return web_search.invoke(f"{import_name} python package pypi install", timeout=timeout)
    except Exception as e:
        print(f"   [DEBUG] _safe_search 异常 {import_name}: {e}")
        return ""


def _extract_pypi_name_from_search(search_result: str, target_import: str) -> Optional[str]:
    if not search_result:
        return None
    matches = re.findall(r'pypi\.org/project/([a-z0-9_-]+)', str(search_result).lower())
    if not matches:
        return None
    target = target_import.lower().replace('_', '-').replace('.', '-')
    for m in matches:
        m_norm = m.lower().replace('_', '-')
        if target in m_norm or m_norm in target:
            return m_norm
    return None


# ==========================================
# 节点 3：Fusion
# ==========================================
def fusion_node(state: ScannerAgentState) -> dict:
    print(f"\n{'='*60}")
    print(f"[DEBUG] >>> 进入 fusion_node")
    
    identified = state.get("identified_packages", [])
    project_path = state.get("project_path_win", "")
    print(f"[DEBUG] identified_packages 数量: {len(identified)}")
    print(f"[DEBUG] project_path: {project_path}")
    
    # 1. 映射表兜底
    print(f"[DEBUG] 开始 _apply_mapping_fallback...")
    identified = _apply_mapping_fallback(identified)
    print(f"[DEBUG] 映射表兜底完成")
    
    # 2. 分离
    third_party = [p for p in identified if p.get("category") == "third_party"]
    internal = [p for p in identified if p.get("category") == "internal"]
    print(f"[DEBUG] third_party: {len(third_party)}, internal: {len(internal)}")
    
    # 3. PyPI 验证
    pypi_names = list({p["pypi_name"] for p in third_party})  # set 去重，避免重复查询
    print(f"[DEBUG] 待 PyPI 验证: {pypi_names}")
    
    try:
        from tools.dependency_resolver import resolve_dependencies
        print(f"[DEBUG] 调用 resolve_dependencies...")
        raw_resolve = resolve_dependencies.invoke({"package_names": pypi_names})
        print(f"[DEBUG] resolve_dependencies 返回类型: {type(raw_resolve)}")
        
        if isinstance(raw_resolve, str):
            resolve_result = json.loads(raw_resolve)
        elif isinstance(raw_resolve, dict):
            resolve_result = raw_resolve
        else:
            resolve_result = {"resolved": [], "failed": pypi_names, "warnings": [f"异常返回类型: {type(raw_resolve)}"]}
    except Exception as e:
        print(f"[DEBUG] ❌ PyPI 验证异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        resolve_result = {"resolved": [], "failed": pypi_names, "warnings": [f"PyPI验证异常: {e}"]}
    
    pypi_exists = {r["original_name"]: r for r in resolve_result.get("resolved", [])}
    pypi_failed = resolve_result.get("failed", [])
    print(f"[DEBUG] PyPI 存在: {len(pypi_exists)}, 失败: {len(pypi_failed)}")
    if pypi_failed:
        print(f"[DEBUG] PyPI 失败列表: {pypi_failed}")
    
    # 4. 初分类
    A_reliable, B_suspect = [], []
    for pkg in third_party:
        name = pkg["pypi_name"]
        if name in pypi_exists:
            A_reliable.append({
                **pkg,
                "canonical_name": pypi_exists[name]["pypi_name"],
                "source": "LLM+PyPI"
            })
        else:
            # PyPI 查询失败（404 或异常）
            # 如果 LLM confidence=high 且包名规范，降级信任（防抖动误杀）
            if pkg.get("confidence") == "high" and re.match(r'^[a-z0-9]+(-[a-z0-9]+)*$', name):
                A_reliable.append({
                    **pkg,
                    "canonical_name": name,
                    "source": "LLM+PyPI(retry-fallback)",
                    "note": "PyPI查询失败，但LLM high confidence且包名规范，降级信任"
                })
                print(f"   [DEBUG] 降级信任: {name}")
            else:
                B_suspect.append(pkg)
    
    # 5. 并发搜索
    to_search = [p for p in B_suspect if p.get("confidence") != "high"]
    print(f"[DEBUG] 需要搜索的 B 类: {len(to_search)}")
    search_results = {}
    if to_search:
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(_safe_search, p["import_name"]): p for p in to_search}
            for fut in as_completed(futures):
                pkg = futures[fut]
                try:
                    search_results[pkg["import_name"]] = fut.result()
                except Exception as e:
                    print(f"[DEBUG] 搜索异常 {pkg['import_name']}: {e}")
                    search_results[pkg["import_name"]] = ""
    
    # 6. B → C_search / 保持 B
    C_search_corrected, B_unverified = [], []
    for pkg in B_suspect:
        sr = search_results.get(pkg["import_name"], "")
        found_name = _extract_pypi_name_from_search(sr, pkg["import_name"])
        if found_name:
            C_search_corrected.append({
                **pkg, "corrected_pypi_name": found_name, "source": "search",
                "confidence": "medium", "note": f"搜索对齐确认: {found_name}"
            })
        else:
            B_unverified.append({**pkg, "source": "LLM-only", "note": "未验证"})
    print(f"[DEBUG] C_search: {len(C_search_corrected)}, B_unverified: {len(B_unverified)}")
    
    # 7. 内部反修正
    C_mapping_corrected = []
    for pkg in internal:
        imp = pkg["import_name"]
        if imp in PYPI_NAME_MAP:
            C_mapping_corrected.append({
                **pkg, "corrected_category": "third_party", "corrected_pypi_name": PYPI_NAME_MAP[imp],
                "source": "mapping", "confidence": "high",
                "note": f"映射表反修正: {PYPI_NAME_MAP[imp]}"
            })
    print(f"[DEBUG] C_mapping: {len(C_mapping_corrected)}")
    
    # 8. 最终清单
    final_deps = set()
    for p in A_reliable:
        final_deps.add(p["canonical_name"])
    for p in C_mapping_corrected:
        final_deps.add(p["corrected_pypi_name"])
    for p in C_search_corrected:
        final_deps.add(p["corrected_pypi_name"])
    print(f"[DEBUG] 最终 deps (fusion): {sorted(final_deps)}")
    
    # === P1: 提取 PyTorch CUDA 版本标记 ===
    pytorch_cuda_version = "12.1"  # 默认
    for dep in final_deps:
        if dep.startswith("torch"):
            m = re.search(r'\+cu(\d{3})', dep)
            if m:
                v = m.group(1)
                pytorch_cuda_version = f"{v[0]}.{v[1:]}"
                print(f"[DEBUG P1] 从 torch 版本提取 CUDA 版本: {pytorch_cuda_version}")
                break
            m2 = re.search(r'cu(\d{3})', dep.lower())
            if m2:
                v = m2.group(1)
                pytorch_cuda_version = f"{v[0]}.{v[1:]}"
                print(f"[DEBUG P1] 从 torch 版本提取 CUDA 版本: {pytorch_cuda_version}")
                break

    # 9. 重依赖分流
    normal, heavy = _split_heavy_packages(list(final_deps))
    print(f"[DEBUG] normal: {len(normal)}, heavy: {len(heavy)}")
    
    # 10. 合并依赖文件（所有包进入 dependencies，不再剔除 heavy）
    analysis = state.get("structure_analysis", {})
    facts = state.get("facts", {})
    print(f"[DEBUG] 调用 extract_dependencies...")
    dep_result = extract_dependencies(str(Path(project_path)), analysis, facts, list(final_deps))
    print(f"[DEBUG] extract_dependencies 完成")

    # heavy 包不进自动安装清单，只进 heavy.json 做提示
    print(f"[DEBUG] heavy 包已分流，共 {len(heavy)} 个，不进入自动安装清单")
    all_heavy = heavy

    # 最终清单：dependencies 只含 normal 包（heavy 已剔除）
    final_normal = normal
    # 按 bare 名去重（sam2 和 sam-2 的 bare 都是 sam2）
    seen_bare = set()
    unique_heavy = []
    for h in all_heavy:
        if h["bare"] not in seen_bare:
            seen_bare.add(h["bare"])
            unique_heavy.append(h)
    all_heavy = unique_heavy
    print(f"[DEBUG] 总 heavy 数量: {len(all_heavy)}")
    if all_heavy:
        print(f"[DEBUG] heavy 列表: {[h['name'] for h in all_heavy]}")

    # === 修改点 1/3：用 all_heavy 的包名填充 risk_packages ===
    heavy_names = [h["name"] for h in all_heavy]

    result = {
        "scan_phase": "extracting",
        "dependencies": normal,  # ← 只存 normal 包，heavy 已剔除
        "heavy_deps": all_heavy,
        "framework": dep_result["framework"],
        "python_version": dep_result["python_version"],
        "system_deps": dep_result["system_deps"],
        "risk_packages": heavy_names,  # ← 修改点 1/3：用分流后的 heavy 包名，不用 LLM 原始空字段
        "pytorch_cuda_version": pytorch_cuda_version,  # ← 新增
        "needs_project_build": facts.get("needs_project_build", False),  # ← 新增
        "verification_result": {
            "A_reliable": A_reliable,
            "B_unverified": B_unverified,
            "C_mapping": C_mapping_corrected,
            "C_search": C_search_corrected,
        },
        "fusion_notes": (
            f"A:{len(A_reliable)}|Cm:{len(C_mapping_corrected)}|"
            f"Cs:{len(C_search_corrected)}|B:{len(B_unverified)}"
        )
    }
    print(f"[DEBUG] <<< 退出 fusion_node")
    print(f"[DEBUG] fusion_notes: {result['fusion_notes']}")
    return result


# ==========================================
# 节点 4：纯函数写文件
# ==========================================
def write_node(state: ScannerAgentState) -> dict:
    print(f"\n{'='*60}")
    print(f"[DEBUG] >>> 进入 write_node")
    
    project_path = state.get("project_path_win", "")
    deps = state.get("dependencies", [])
    heavy = state.get("heavy_deps", [])

    # 双重保险：确保 requirements_agent.txt 不含 heavy 包
    heavy_names = {h["name"] for h in heavy}
    filtered_deps = [d for d in deps if d not in heavy_names]
    if len(filtered_deps) < len(deps):
        print(f"[DEBUG] write_node 过滤掉 heavy 包: {set(deps) - set(filtered_deps)}")
    deps = filtered_deps
    
    analysis = state.get("structure_analysis", {})
    verify = state.get("verification_result", {})
    
    
    print(f"[DEBUG] project_path: {project_path}")
    print(f"[DEBUG] deps 数量: {len(deps)}")
    print(f"[DEBUG] heavy 数量: {len(heavy)}")
    
    if not project_path:
        print(f"[DEBUG] ❌ project_path 为空")
        return {"verify_ok": False, "error": "project_path_win 为空", "scan_phase": "error"}
    
    req_path = os.path.join(project_path, "requirements_agent.txt")
    print(f"[DEBUG] 目标文件: {req_path}")
    
    # ========== 插入在这里：替换原来的 lines = list(deps) ==========
    identified = state.get("identified_packages", [])
    import_map = {p["pypi_name"]: p["import_name"] for p in identified 
                  if p.get("category") == "third_party" and p.get("pypi_name") and p.get("import_name")}

    lines = []
    for dep in deps:
        imp = import_map.get(dep)
        if imp and imp != dep:
            lines.append(f"{dep}  # import: {imp}")
        else:
            lines.append(dep)
    
    if not lines:
        lines = ["# No third-party dependencies detected."]
    lines.append("")
    lines.append("# --- Scanner Metadata ---")
    lines.append(f"# Framework: {analysis.get('framework', 'unknown')}")
    lines.append(f"# Python: {analysis.get('python_version', '')}")
    lines.append(f"# System deps: {', '.join(analysis.get('system_deps', []))}")

     # Heavy 包提示：写入注释，用户打开文件就能看到
    if heavy:
        lines.append("")
        lines.append("# --- Heavy Packages (Skipped Auto-Install) ---")
        for h in heavy:
            meta = h.get("meta", {})
            lines.append(f"# {h['name']}: {meta.get('reason', '需特殊处理')}")
            if meta.get("fix_hint"):
                lines.append(f"#   安装示例: {meta['fix_hint']}")
        print(f"[DEBUG] Heavy packages 注释已写入: {[h['name'] for h in heavy]}")
    
    # === 修改点 2/3：从 state 读取 risk_packages 写入注释 ===
    risk_packages = state.get("risk_packages", [])
    if risk_packages:
        lines.append(f"# Risk packages: {', '.join(risk_packages)}")
        print(f"[DEBUG] Risk packages: {', '.join(risk_packages)}")
    
    C_mapping = verify.get("C_mapping", [])
    if C_mapping:
        names = ", ".join([p["corrected_pypi_name"] for p in C_mapping])
        lines.append(f"# MAPPING-VERIFIED: {names}")
        print(f"[DEBUG] MAPPING-VERIFIED: {names}")
    
    C_search = verify.get("C_search", [])
    if C_search:
        names = ", ".join([p["corrected_pypi_name"] for p in C_search])
        lines.append(f"# SEARCH-VERIFIED (review needed): {names}")
        print(f"[DEBUG] SEARCH-VERIFIED: {names}")
    
    B_unverified = verify.get("B_unverified", [])
    if B_unverified:
        names = ", ".join([p["import_name"] for p in B_unverified])
        lines.append(f"# UNVERIFIED (manual check needed): {names}")
        print(f"[DEBUG] UNVERIFIED: {names}")
    
    content = "\n".join(lines)
    print(f"[DEBUG] 文件内容预览（前500字符）:\n{content[:500]}")
    
    try:
        with open(req_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[DEBUG] ✅ 写入成功: {req_path}")
    except Exception as e:
        print(f"[DEBUG] ❌ 写入失败: {e}")
        traceback.print_exc()
        return {"verify_ok": False, "error": f"写入失败: {e}", "scan_phase": "extracting"}
    
    if heavy and project_path:
        heavy_path = os.path.join(project_path, "requirements_heavy.json")
        try:
            with open(heavy_path, "w", encoding="utf-8") as f:
                json.dump(heavy, f, ensure_ascii=False, indent=2)
            print(f"[DEBUG] ✅ 写入 heavy.json: {heavy_path}")
        except Exception as e:
            print(f"[DEBUG] ⚠️ 写入 heavy.json 失败: {e}")
    
    result = {
        "requirements_path": req_path,
        "scan_phase": "written",
        "verify_ok": True,
        "error": ""
    }
    print(f"[DEBUG] <<< 退出 write_node")
    return result


# ==========================================
# 节点 5：文件校验
# ==========================================
def verify_file_node(state: ScannerAgentState) -> dict:
    print(f"\n{'='*60}")
    print(f"[DEBUG] >>> 进入 verify_file_node")
    
    req_path = state.get("requirements_path", "")
    print(f"[DEBUG] 检查文件: {req_path}")
    
    if not req_path:
        print(f"[DEBUG] ❌ requirements_path 为空")
        return {"verify_ok": False, "error": "requirements_path 为空", "scan_phase": "extracting"}
    
    exists = os.path.exists(req_path)
    print(f"[DEBUG] 文件存在: {exists}")
    
    if exists:
        try:
            size = os.path.getsize(req_path)
            print(f"[DEBUG] 文件大小: {size} bytes")
        except Exception as e:
            print(f"[DEBUG] 获取文件大小失败: {e}")
        
        result = {"verify_ok": True, "scan_phase": "written", "error": ""}
        print(f"[DEBUG] <<< 退出 verify_file_node, verify_ok=True")
        return result
    else:
        result = {"verify_ok": False, "error": f"文件未成功写入: {req_path}", "scan_phase": "extracting"}
        print(f"[DEBUG] <<< 退出 verify_file_node, verify_ok=False")
        return result


# ==========================================
# 节点 6：修复分支
# ==========================================
def fix_node(state: ScannerAgentState) -> dict:
    print(f"\n{'='*60}")
    print(f"[DEBUG] >>> 进入 fix_node")
    
    retries = state.get("retry_count", 0)
    error = state.get("error", "")
    phase = state.get("scan_phase", "identifying")
    facts = state.get("facts", {})
    
    print(f"[DEBUG] retry_count={retries}, error={error!r}, phase={phase}")
    
    if retries >= 3:
        print(f"[DEBUG] ⚠️ 已达最大重试次数，fallback 到纯函数盲扫")
        fallback = get_fallback_analysis(facts)
        identified = fallback.get("identified_packages", [])
        print(f"[DEBUG] fallback identified_packages: {len(identified)}")
        
        result = {
            "retry_count": retries + 1,
            "fix_strategy": "fallback_to_fusion",
            "identified_packages": identified,
            "structure_analysis": fallback,
            "scan_phase": "extracting",
            "error": "",
        }
        print(f"[DEBUG] <<< 退出 fix_node, strategy=fallback_to_fusion")
        return result

    result = {
        "retry_count": retries + 1,
        "fix_strategy": "retry",
        "scan_phase": phase,
        "error": "",
    }
    print(f"[DEBUG] <<< 退出 fix_node, strategy=retry, phase={phase}")
    return result


# ==========================================
# 节点 7：提取结果
# ==========================================
def extract_result(state: ScannerAgentState) -> dict:
    print(f"\n{'='*60}")
    print(f"[DEBUG] >>> 进入 extract_result")
    
    req_path = state.get("requirements_path", "")
    project_path = state.get("project_path_win", "")
    analysis = state.get("structure_analysis", {})
    heavy = state.get("heavy_deps", [])
    fusion_notes = state.get("fusion_notes", "")
    
    if not req_path and project_path:
        req_path = os.path.join(project_path, "requirements_agent.txt")
    
    heavy_names = [h["name"] for h in heavy]
    heavy_str = f" | 需编译包({len(heavy_names)}): {', '.join(heavy_names)}" if heavy_names else ""
    
    # === 修改点 3/3：risk 从 state 读取，不用 analysis ===
    summary_text = (
        f"✅ 依赖分析完成。\n"
        f"requirements: {req_path}\n"
        f"framework: {analysis.get('framework', 'unknown')}\n"
        f"python: {analysis.get('python_version', '')}\n"
        f"system_deps: {', '.join(analysis.get('system_deps', []))}\n"
        f"risk: {', '.join(state.get('risk_packages', []))}\n"
        f"fusion: {fusion_notes}"
        f"{heavy_str}"
    )
    print(f"[DEBUG] summary:\n{summary_text}")
    
    summary = AIMessage(content=summary_text)

    timestamps = state.get("timestamps", {})
    timestamps["scanner_end"] = time.time()
    elapsed = timestamps["scanner_end"] - timestamps["scanner_start"]
    print(f"\n⏱️ [Scanner] 阶段耗时: {elapsed:.1f}s")
    
    result = {
        "requirements_path": req_path,
        "messages": [summary],
        "next_agent": "docker_builder",
        "current_agent": "scanner",
        "scan_phase": "written",
        "skipped_packages": [],  # ← 新策略：不再跳过任何包，heavy 也进入安装流程
        "needs_project_build": state.get("facts", {}).get("needs_project_build", False), 
        "timestamps": timestamps,
    }
    print(f"[DEBUG] <<< 退出 extract_result")
    return result


# ==========================================
# 条件边（带调试）
# ==========================================
def _precheck_router(state: ScannerAgentState) -> str:
    ok = state.get("precheck_ok")
    print(f"[DEBUG ROUTER] precheck → {'identify' if ok else 'fix'} (precheck_ok={ok})")
    return "identify" if ok else "fix"

def _verify_schema_router(state: ScannerAgentState) -> str:
    ok = state.get("verify_ok")
    print(f"[DEBUG ROUTER] verify_schema → {'fusion' if ok else 'fix'} (verify_ok={ok})")
    return "fusion" if ok else "fix"

def _verify_file_router(state: ScannerAgentState) -> str:
    ok = state.get("verify_ok")
    print(f"[DEBUG ROUTER] verify_file → {'extract' if ok else 'fix'} (verify_ok={ok})")
    return "extract" if ok else "fix"

def _fix_router(state: ScannerAgentState) -> str:
    strategy = state.get("fix_strategy", "")
    print(f"[DEBUG ROUTER] fix → {strategy} (fix_strategy={strategy!r})")
    if strategy == "fallback_to_fusion":
        return "fusion"
    elif strategy == "retry":
        return "identify"
    return END


# ==========================================
# 构建子图
# ==========================================
def create_scanner_agent():
    workflow = StateGraph(ScannerAgentState)

    workflow.add_node("precheck", precheck_node)
    workflow.add_node("identify", call_model)
    workflow.add_node("verify_schema", verify_schema_node)
    workflow.add_node("fusion", fusion_node)
    workflow.add_node("write", write_node)
    workflow.add_node("verify_file", verify_file_node)
    workflow.add_node("fix", fix_node)
    workflow.add_node("extract", extract_result)

    workflow.set_entry_point("precheck")
    
    workflow.add_conditional_edges(
        "precheck",
        _precheck_router,
        {"identify": "identify", "fix": "fix"}
    )
    
    workflow.add_edge("identify", "verify_schema")
    
    workflow.add_conditional_edges(
        "verify_schema",
        _verify_schema_router,
        {"fusion": "fusion", "fix": "fix"}
    )
    
    workflow.add_edge("fusion", "write")
    workflow.add_edge("write", "verify_file")
    
    workflow.add_conditional_edges(
        "verify_file",
        _verify_file_router,
        {"extract": "extract", "fix": "fix"}
    )
    
    workflow.add_conditional_edges(
        "fix",
        _fix_router,
        {"fusion": "fusion", "identify": "identify", END: END}
    )
    
    workflow.add_edge("extract", END)
    return workflow.compile()


scanner_agent_node = create_scanner_agent()