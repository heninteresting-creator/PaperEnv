"""
Scanner 2.1 技能函数 —— 鲁棒底座 + 契约防护 + 精准提取
"""

import os
import ast
import json
import re
import shlex
from pathlib import Path
from typing import Dict, Any, List, Tuple, Set, Optional

# ---------- Pydantic Schema（契约防护） ----------
try:
    from pydantic import BaseModel, Field, validator
    from typing import Literal

    HAS_PYDANTIC = True

    class DepFile(BaseModel):
        path: str
        priority: int = Field(..., ge=1, le=10)
        reason: str

    class StructureAnalysis(BaseModel):
        dependency_files: List[DepFile]
        framework: Literal["pytorch", "tensorflow", "jax", "pure_python", "unknown"]
        python_version: Optional[str] = Field(default=None)  # ← 接受 None，不再强制 pattern
        system_deps: List[str] = Field(default_factory=list)
        identified_packages: List[Dict[str, Any]] = Field(default_factory=list)
        risk_packages: List[str] = Field(default_factory=list)
        notes: str = Field(default="")

        @validator("dependency_files")
        def check_paths(cls, v):
            for f in v:
                if ".." in f.path or f.path.startswith("/") or f.path.startswith("\\"):
                    raise ValueError(f"非法路径: {f.path}")
            return v

except ImportError:
    HAS_PYDANTIC = False
    from dataclasses import dataclass, field

    @dataclass
    class DepFile:
        path: str
        priority: int = 1
        reason: str = ""

    @dataclass
    class StructureAnalysis:
        dependency_files: List[DepFile] = field(default_factory=list)
        framework: str = "unknown"
        python_version: str = ""
        system_deps: List[str] = field(default_factory=list)
        identified_packages: List[Dict[str, Any]] = field(default_factory=list)
        risk_packages: List[str] = field(default_factory=list)
        notes: str = ""


# ---------- packaging 裸名清洗 ----------
try:
    from packaging.requirements import Requirement

    def clean_package_name(dep: str) -> str:
        try:
            return Requirement(dep).name
        except Exception:
            return _fallback_clean(dep)

except ImportError:
    def clean_package_name(dep: str) -> str:
        return _fallback_clean(dep)


def _fallback_clean(dep: str) -> str:
    bare = dep.split("==")[0].split(">=")[0].split("<=")[0].split(";")[0].strip()
    # PEP 503 规范化：连续 -_. 统一转 -，全小写
    bare = re.sub(r'[-_.]+', '-', bare).lower()
    return bare


# ---------- 常量 ----------
IGNORE_DIRS = {
    ".git", "venv", "__pycache__", "node_modules", ".idea",
    "build", "dist", ".egg-info", ".tox", ".pytest_cache",
    "htmlcov", ".mypy_cache", ".coverage"
}
MAX_DEPTH = 3
MAX_PY_FILES = 100

# 标准库列表（最终兜底，防止 LLM 误判）
STD_LIBS = {
    "abc", "argparse", "ast", "atexit", "base64", "binascii", "bisect",
    "builtins", "collections", "concurrent", "contextlib", "copy", "csv",
    "dataclasses", "datetime", "decimal", "difflib", "dis", "doctest",
    "email", "enum", "errno", "faulthandler", "filecmp", "fnmatch",
    "fractions", "ftplib", "functools", "gc", "getopt", "gettext",
    "glob", "graphlib", "gzip", "hashlib", "heapq", "imaplib",
    "imghdr", "importlib", "inspect", "io", "ipaddress", "itertools",
    "json", "keyword", "lib2to3", "linecache", "locale", "logging",
    "mailbox", "math", "mimetypes", "modulefinder", "multiprocessing",
    "netrc", "nntplib", "numbers", "operator", "optparse", "os",
    "pathlib", "pdb", "pickle", "pkgutil", "platform", "plistlib",
    "poplib", "posixpath", "pprint", "profile", "pstats", "pwd",
    "py_compile", "queue", "quopri", "random", "re", "reprlib",
    "runpy", "sched", "secrets", "select", "selectors", "shelve",
    "shlex", "shutil", "signal", "site", "smtpd", "smtplib", "sndhdr",
    "socket", "socketserver", "spwd", "sqlite3", "ssl", "stat",
    "statistics", "string", "stringprep", "struct", "subprocess",
    "sunau", "symtable", "sys", "sysconfig", "tabnanny", "tarfile",
    "telnetlib", "tempfile", "textwrap", "threading", "time", "timeit",
    "token", "tokenize", "trace", "tracemalloc", "tty", "turtle",
    "turtledemo", "types", "typing", "unicodedata", "unittest", "uu",
    "urllib", "uuid", "warnings", "wave", "weakref", "webbrowser",
    "xml", "xdrlib", "zipfile", "zipimport", "zoneinfo", "_thread",
    "__future__", "asyncio",
}


# ==========================================
# 1. 鲁棒底座：collect_facts
# ==========================================
def collect_facts(project_path: str) -> Dict[str, Any]:
    print(f"DEBUG collect_facts running for {project_path}")
    facts = {
        "project_path": project_path,
        "file_tree": [],
        "detected_dependency_files": [],
        "dependency_file_snippets": {},
        "imports": set(),           # ← 原始 AST imports（不过滤，全部保留）
        "notebook_imports": set(),
        "readme_snippet": "",
        "setup_py_deps": [],
        "setup_py_parse_failed": False,
        "failed_files": [],
        "internal_modules": [],     # ← 供 LLM 参考，不用于硬过滤
    }

    base = Path(project_path).resolve()

    # 1. 文件树 + 依赖文件检测
    for root, dirs, files in os.walk(project_path, topdown=True):
        dirs[:] = [
            d for d in dirs
            if d not in IGNORE_DIRS
            and not os.path.islink(os.path.join(root, d))
        ]

        rel_root = Path(root).relative_to(base).as_posix()
        depth = len(Path(rel_root).parts) if rel_root != "." else 0

        if depth > MAX_DEPTH:
            dirs[:] = []
            continue

        for f in files:
            if f.startswith("."):
                continue
            rel_path = (Path(rel_root) / f).as_posix() if rel_root != "." else f
            facts["file_tree"].append(rel_path)

            if f in (
                "requirements.txt", "setup.py", "pyproject.toml",
                "environment.yml", "poetry.lock", "Pipfile"
            ):
                facts["detected_dependency_files"].append(rel_path)
                try:
                    with open(
                        os.path.join(root, f), "r",
                        encoding="utf-8", errors="ignore"
                    ) as fh:
                        lines = fh.readlines()[:50]
                        facts["dependency_file_snippets"][rel_path] = "".join(lines)
                except Exception as e:
                    facts["failed_files"].append(f"{rel_path}: read_error {e}")

    # 2. AST 解析 .py 文件（逐文件 try/except）
    py_files = [p for p in facts["file_tree"] if p.endswith(".py")]
    py_files.sort(key=lambda p: (len(Path(p).parts), p))
    py_files = py_files[:MAX_PY_FILES]

    for rel_path in py_files:
        full_path = base / rel_path
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        facts["imports"].add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        facts["imports"].add(node.module.split(".")[0])
        except SyntaxError:
            pass
        except Exception as e:
            facts["failed_files"].append(f"{rel_path}: ast_error {e}")

    # 3. setup.py AST 两级提取（只取 install_requires）
    setup_py_path = next(
        (p for p in facts["detected_dependency_files"] if p.endswith("setup.py")),
        None
    )
    if setup_py_path:
        content = facts["dependency_file_snippets"].get(setup_py_path, "")
        deps, ok = extract_setup_py_deps(content)
        if ok:
            facts["setup_py_deps"] = deps
        else:
            facts["setup_py_parse_failed"] = True

    # 4. .ipynb 轻量提取（!pip install + import）
    notebook_files = [p for p in facts["file_tree"] if p.endswith(".ipynb")]
    for rel_path in notebook_files[:5]:
        full_path = base / rel_path
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                nb = json.load(f)
            for cell in nb.get("cells", []):
                if cell.get("cell_type") == "code":
                    source = "".join(cell.get("source", []))
                    for m in re.finditer(r"!pip\s+install\s+(.*?)(?:;|$)", source, re.M):
                        args = m.group(1).strip()
                        try:
                            tokens = shlex.split(args)
                            for tok in tokens:
                                if tok.startswith("-"):
                                    continue
                                facts["notebook_imports"].add(
                                    tok.split("==")[0].split(">=")[0].strip()
                                )
                        except ValueError:
                            pass
                    for m in re.finditer(
                        r"^\s*(?:from|import)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
                        source, re.M
                    ):
                        facts["notebook_imports"].add(m.group(1))
        except Exception:
            pass

    # 5. README 相关段落提取
    readme_candidates = [
        p for p in facts["file_tree"]
        if p.lower().startswith("readme") and not p.endswith(".png")
    ]
    if readme_candidates:
        try:
            with open(base / readme_candidates[0], "r",
                      encoding="utf-8", errors="ignore") as f:
                content = f.read()
            lines = content.split("\n")
            snippet_lines = []
            for i, line in enumerate(lines):
                if any(kw in line.lower() for kw in [
                    "requirement", "install", "dependency",
                    "python", "cuda", "setup", "environment"
                ]):
                    start = max(0, i - 2)
                    end = min(len(lines), i + 3)
                    snippet_lines.extend(lines[start:end])
                    snippet_lines.append("---")
            facts["readme_snippet"] = "\n".join(snippet_lines[:30])
        except Exception:
            pass

    # 6. 提取依赖文件中的裸包名（供 LLM 统一判断）
    file_packages = set()
    for dep_file in facts.get("detected_dependency_files", []):
        full_path = base / dep_file
        if not full_path.exists():
            continue
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if dep_file.endswith("requirements.txt"):
                file_packages.update(parse_requirements_txt(content))
            elif dep_file == "pyproject.toml":
                file_packages.update(parse_pyproject_toml(content))
        except Exception:
            pass
    
    # 合并 setup.py AST 结果（清洗版本号）
    for dep in facts.get("setup_py_deps", []):
        file_packages.add(clean_package_name(dep))
    
    facts["file_packages"] = sorted(file_packages)

    # 6. 收集内部模块候选（所有目录名 + 所有 .py 文件名 + 项目根目录名）
    internal_modules = set()
    for p in facts["file_tree"]:
        if not p:
            continue
        parts = p.split("/")
        for part in parts[:-1]:
            internal_modules.add(part.lower())
        if p.endswith(".py"):
            internal_modules.add(Path(p).stem.lower())
    internal_modules.add(Path(project_path).name.lower())
    facts["internal_modules"] = sorted(internal_modules)

    # 序列化
    facts["imports"] = sorted(facts["imports"])
    facts["notebook_imports"] = sorted(facts["notebook_imports"])
    facts["file_tree"] = "\n".join(facts["file_tree"][:200])
    return facts


# ==========================================
# 2. setup.py AST 提取（只取 install_requires）
# ==========================================
def extract_setup_py_deps(content: str) -> Tuple[List[str], bool]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return [], False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_setup = (
                (isinstance(func, ast.Name) and func.id == "setup")
                or (isinstance(func, ast.Attribute) and func.attr == "setup")
            )
            if not is_setup:
                continue

            deps: List[str] = []
            for kw in node.keywords:
                if kw.arg == "install_requires":
                    if isinstance(kw.value, (ast.List, ast.Tuple)):
                        for elt in kw.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                deps.append(elt.value)
                    else:
                        return [], False
            return deps, True if deps else False
    return [], False


# ==========================================
# 3. 契约防护：verify_llm_analysis（适配 identified_packages）
# ==========================================
def verify_llm_analysis(
    raw_content: str, facts: Dict[str, Any]
) -> Tuple[bool, Dict[str, Any], str]:
    try:
        json_str = _extract_json_from_markdown(raw_content)
        data = json.loads(json_str)

        # 清洗 python_version：LLM 常输出 "3.8+" / ">=3.9"，截断为合法版本号
        raw_pyver = data.get("python_version", "")
        if raw_pyver:
            m = re.search(r'^(\d+\.\d+(?:\.\d+)?)', str(raw_pyver))
            cleaned = m.group(1) if m else ""
            if cleaned != raw_pyver:
                print(f"   [DEBUG] 清洗 python_version: {raw_pyver!r} → {cleaned!r}")
            data["python_version"] = cleaned
        else:
            data["python_version"] = ""
            print(f"   [DEBUG] 清洗 python_version: None → ''")

        if HAS_PYDANTIC:
            if hasattr(StructureAnalysis, "model_validate"):
                analysis = StructureAnalysis.model_validate(data)
                analysis_dict = analysis.model_dump()
            else:
                analysis = StructureAnalysis.parse_obj(data)
                analysis_dict = analysis.dict()
        else:
            analysis_dict = {
                "dependency_files": data.get("dependency_files", []),
                "framework": data.get("framework", "unknown"),
                "python_version": data.get("python_version", ""),
                "system_deps": data.get("system_deps", []),
                "identified_packages": data.get("identified_packages", []),
                "notes": data.get("notes", ""),
            }

        # 路径交叉验证
        valid_files = []
        file_tree_set = set(facts.get("file_tree", "").split("\n"))
        for f in analysis_dict["dependency_files"]:
            path = f.get("path", "")
            if ".." in path or path.startswith("/") or path.startswith("\\"):
                continue
            full_path = Path(facts["project_path"]) / path
            if full_path.exists() or path in file_tree_set:
                valid_files.append(f)

        analysis_dict["dependency_files"] = valid_files

        # 保底：空列表时注入已知文件
        if not analysis_dict["dependency_files"]:
            fallback = []
            for i, known in enumerate(facts.get("detected_dependency_files", [])):
                fallback.append({
                    "path": known,
                    "priority": i + 1,
                    "reason": "LLM未识别，自动保底"
                })
            analysis_dict["dependency_files"] = fallback
            analysis_dict["notes"] += " [保底触发：LLM未识别依赖文件]"

        # framework 归一化
        fw = analysis_dict.get("framework", "").lower().strip()
        if fw in ("pytorch", "torch"):
            analysis_dict["framework"] = "pytorch"
        elif fw in ("tensorflow", "tf"):
            analysis_dict["framework"] = "tensorflow"
        elif fw in ("jax", "flax"):
            analysis_dict["framework"] = "jax"
        elif fw in ("pure_python", "pure", "none", ""):
            analysis_dict["framework"] = "pure_python"
        else:
            analysis_dict["framework"] = "unknown"

        # 新增：identified_packages 数量完整性校验
        raw_imports = set(facts.get("imports", []))
        identified = analysis_dict.get("identified_packages", [])
        identified_imports = {p.get("import_name") for p in identified if p.get("import_name")}
        
        # 如果 LLM 漏了超过 20% 的 import，标记警告（不阻断，但记 notes）
        if raw_imports:
            missed = raw_imports - identified_imports
            if len(missed) / len(raw_imports) > 0.2:
                analysis_dict["notes"] += f" [警告：LLM 漏识别 {len(missed)} 个 import]"

        return True, analysis_dict, ""

    except Exception as e:
        return False, {}, str(e)


def _extract_json_from_markdown(text: str) -> str:
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text.strip()


# ==========================================
# 4. 精准提取：parse_requirements_txt + pyproject.toml
# ==========================================
def parse_requirements_txt(content: str) -> Set[str]:
    deps: Set[str] = set()
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if line.startswith("git+") or line.startswith("http"):
            match = re.search(r"/([^/]+?)(?:\.git)?(?:[@#].*)?$", line)
            if match:
                deps.add(match.group(1))
            continue
        name = clean_package_name(line)
        if name:
            deps.add(name)
    return deps


def parse_pyproject_toml(content: str) -> Set[str]:
    deps: Set[str] = set()
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            tomllib = None

    if tomllib:
        try:
            data = tomllib.loads(content)
            project_deps = data.get("project", {}).get("dependencies", [])
            for dep in project_deps:
                deps.add(clean_package_name(dep))
            return deps
        except Exception:
            pass

    in_deps = False
    for ln in content.split("\n"):
        if "dependencies" in ln and "[" in ln:
            in_deps = True
            continue
        if in_deps:
            if ln.strip().startswith("["):
                break
            for m in re.finditer(r'"([^"]+)"', ln):
                deps.add(clean_package_name(m.group(1)))
            for m in re.finditer(r"'([^']+)'", ln):
                deps.add(clean_package_name(m.group(1)))
    return deps


# ==========================================
# 5. 依赖提取：合并 fusion 结果 + 文件解析（无硬映射/硬过滤）
# ==========================================
def extract_dependencies(
    project_path: str,
    analysis: Dict[str, Any],
    facts: Dict[str, Any],
    fusion_deps: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    清洗 fusion 验证结果，审计文件遗漏，重依赖分流。
    不再从依赖文件解析新包名（LLM 已在 identify 阶段统一处理）。
    """
    all_deps: Set[str] = set()
    if fusion_deps:
        all_deps.update(fusion_deps)

    # 1. 最终清洗：PEP 503 规范化 + 标准库兜底
    cleaned: Set[str] = set()
    for d in all_deps:
        bare = clean_package_name(d)
        bare = re.sub(r'[-_.]+', '-', bare).lower().strip()
        if bare and bare not in STD_LIBS:
            cleaned.add(bare)

    # 2. 差异审计：文件声明但 LLM 未识别的包（不自动纳入，只告警）
    file_packages = set(facts.get("file_packages", []))
    llm_packages = cleaned
    missed = file_packages - llm_packages - STD_LIBS
    warnings = [f"文件声明但LLM未识别: {pkg}" for pkg in sorted(missed)]

    # 3. 重依赖分流
    try:
        from tools.dependency_resolver import _is_heavy_package
    except ImportError:
        _is_heavy_package = lambda x: None

    normal, heavy = [], []
    for pkg in cleaned:
        meta = _is_heavy_package(pkg)
        if meta:
            heavy.append({"name": pkg, "bare": pkg, "meta": meta})
        else:
            normal.append(pkg)

    return {
        "dependencies": sorted(normal),
        "heavy_deps": heavy,
        "fusion_warnings": warnings,
        "framework": analysis.get("framework", "unknown"),
        "python_version": analysis.get("python_version", ""),
        "system_deps": analysis.get("system_deps", []),
        "risk_packages": analysis.get("risk_packages", []),
    }

# ==========================================
# 6. Fallback：纯函数盲扫（生成 identified_packages 格式）
# ==========================================
def get_fallback_analysis(facts: Dict[str, Any]) -> Dict[str, Any]:
    deps: Set[str] = set()
    for known in facts.get("detected_dependency_files", []):
        if known.endswith("requirements.txt"):
            full_path = Path(facts["project_path"]) / known
            if full_path.exists():
                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                        deps.update(parse_requirements_txt(f.read()))
                except Exception:
                    pass

    fallback_files = []
    for i, known in enumerate(facts.get("detected_dependency_files", [])):
        fallback_files.append({
            "path": known,
            "priority": i + 1,
            "reason": "LLM未识别，自动保底"
        })

    # 从 imports 构造 identified_packages（盲扫格式，全部标记为 third_party）
    raw_imports = set(facts.get("imports", []))
    identified = []
    for imp in raw_imports:
        if imp in STD_LIBS:
            continue  # 标准库不进入 identified_packages
        identified.append({
            "import_name": imp,
            "pypi_name": imp,
            "category": "third_party",
            "reason": "纯函数盲扫，无 LLM 知识验证",
            "confidence": "low"
        })

    return {
        "dependency_files": fallback_files,
        "framework": "unknown",
        "python_version": "",
        "system_deps": [],
        "identified_packages": identified,
        "notes": "LLM分析失败，降级为纯函数盲扫"
    }