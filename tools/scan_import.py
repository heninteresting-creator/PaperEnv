import os
import ast
import sys
from pathlib import Path
from typing import Dict, List, Set, Any, Optional
from langchain_core.tools import tool

# ============================================================
# 1. toml 库兼容性处理
# ============================================================
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

# ============================================================
# 2. 路径转换工具
# ============================================================
def convert_wsl_to_windows(path_str: str) -> str:
    """如果当前在 Windows 运行，将 /mnt/d/... 转换为 d:/..."""
    if os.name == 'nt' and path_str.startswith('/mnt/'):
        parts = path_str.split('/')
        if len(parts) >= 3:
            drive = parts[2]
            rest = '/'.join(parts[3:])
            return f"{drive}:/{rest}"
    return path_str

# ============================================================
# 3. 常量定义
# ============================================================
COMMON_INTERNAL_NAMES = {
    'utils', 'tools', 'common', 'helpers', 'config', 'settings',
    'constants', 'models', 'database', 'services', 'controllers',
    'routes', 'middleware', 'schemas', 'core', 'base', 'mixins',
    'exceptions', 'logger', 'cli', 'scripts', 'tests', 'conftest',
    'app', 'application', 'main', 'run', 'manage', 'wsgi', 'asgi',
}

# 扫描时忽略的目录（含素材/数据目录，防止误判）
IGNORED_DIRS = {
    '.git', '__pycache__', 'node_modules', '.tox', 'dist', 'build',
    '.egg-info', '.eggs', '.mypy_cache', '.pytest_cache',
    'venv', '.venv', 'env', '.env', '.idea', '.vscode',
    'htmlcov', '.coverage', 'site-packages', 'lib', 'libs',
    'bin', 'include', 'share',
    'assets', 'static', 'templates', 'images', 'videos', 'audio',
    'data', 'datasets', 'raw_data', 'processed_data',
    'docs', 'doc', 'documentation', 'examples', 'demo', 'notebooks',
    'uploads', 'media', 'public', 'out', 'target',
}

# 导入名 → PyPI 规范名映射（解决 PIL/Pillow 重复等问题）
BUILTIN_MAPPING = {
    "PIL": "Pillow",
    "pil": "Pillow",
    "Image": "Pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "yaml": "PyYAML",
    "git": "GitPython",
    "jinja2": "Jinja2",
    "markupsafe": "MarkupSafe",
    "psycopg2": "psycopg2-binary",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "jwt": "PyJWT",
    "lxml": "lxml",
    "nltk": "nltk",
    "spacy": "spacy",
    "wandb": "wandb",
    "comet_ml": "comet_ml",
    "Crypto": "pycryptodome",
    "MySQLdb": "mysqlclient",
    "pymysql": "PyMySQL",
    "flask": "Flask",
    "django": "Django",
    "fastapi": "fastapi",
    "sqlalchemy": "SQLAlchemy",
    "pandas": "pandas",
    "numpy": "numpy",
    "matplotlib": "matplotlib",
    "scipy": "scipy",
    "tensorflow": "tensorflow",
    "torch": "torch",
    "transformers": "transformers",
    "pytz": "pytz",
    "requests": "requests",
    "urllib3": "urllib3",
    "click": "click",
    "markdown": "Markdown",
    "redis": "redis",
    "celery": "celery",
    "pytest": "pytest",
    "sphinx": "Sphinx",
    "twisted": "Twisted",
    "html5lib": "html5lib",
    "openpyxl": "openpyxl",
    "xlrd": "xlrd",
    "xlsxwriter": "XlsxWriter",
}

# ============================================================
# 4. 目录过滤
# ============================================================
def _is_venv_or_irrelevant(dirpath: Path) -> bool:
    """判断目录是否属于虚拟环境或无关目录"""
    if (dirpath / 'pyvenv.cfg').exists():
        return True
    if (dirpath / 'lib' / 'python').exists() or \
       (dirpath / 'Lib' / 'site-packages').exists():
        return True
    if dirpath.name in IGNORED_DIRS:
        return True
    if dirpath.name.startswith('venv') or dirpath.name.startswith('.venv'):
        return (dirpath / 'pyvenv.cfg').exists() or \
               (dirpath / 'Scripts' / 'activate.bat').exists() or \
               (dirpath / 'bin' / 'activate').exists()
    return False

# ============================================================
# 5. 高效遍历 Python 文件
# ============================================================
def _walk_python_files(root: Path, max_depth: Optional[int] = None) -> List[Path]:
    """高效遍历，提前跳过无关目录"""
    py_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_path = Path(dirpath).relative_to(root)
        depth = len(rel_path.parts) if str(rel_path) != '.' else 0
        
        if max_depth is not None and depth >= max_depth:
            dirnames[:] = []
            continue
        
        original_dirs = list(dirnames)
        dirnames[:] = []
        for d in original_dirs:
            if d not in IGNORED_DIRS and not d.startswith('.'):
                full_d = Path(dirpath) / d
                if not _is_venv_or_irrelevant(full_d):
                    dirnames.append(d)
        
        for f in filenames:
            if f.endswith('.py'):
                py_files.append(Path(dirpath) / f)
    
    return py_files

# ============================================================
# 6. 标准包检测（有 __init__.py）
# ============================================================
def _find_package_roots(root: Path) -> Set[Path]:
    """找到所有含有 __init__.py 的目录（真实包根）"""
    packages = set()
    for py_file in _walk_python_files(root):
        if py_file.name == "__init__.py":
            packages.add(py_file.parent)
    return packages

def _collect_modules_from_packages(root: Path, packages: Set[Path]) -> Set[str]:
    """将包目录路径转换为模块名，同时收集单层名字"""
    internal = set()
    for pkg_dir in packages:
        try:
            rel = pkg_dir.relative_to(root)
            parts = rel.parts
            for i in range(len(parts)):
                internal.add(".".join(parts[:i+1]))
            for part in parts:
                internal.add(part)
        except ValueError:
            internal.add(pkg_dir.name)
    return internal

# ============================================================
# 7. 命名空间包检测（PEP 420，无 __init__.py）
# ============================================================
def _find_namespace_packages(root: Path) -> Set[str]:
    """找命名空间包：根目录下所有一级候选目录统一递归"""
    namespace_pkgs = set()

    candidates = []
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name.startswith('.') or entry.name in IGNORED_DIRS:
            continue
        candidates.append(entry)

    for candidate in candidates:
        for dirpath, dirnames, filenames in os.walk(candidate):
            dirnames[:] = [
                d for d in dirnames
                if d not in IGNORED_DIRS and not d.startswith('.')
            ]

            current_dir = Path(dirpath)
            has_py = any(f.endswith('.py') for f in filenames)
            has_init = "__init__.py" in filenames

            if has_py and not has_init:
                try:
                    rel = current_dir.relative_to(root)
                    parts = rel.parts
                    for i in range(len(parts)):
                        namespace_pkgs.add(".".join(parts[:i+1]))
                    for part in parts:
                        namespace_pkgs.add(part)
                except ValueError:
                    namespace_pkgs.add(current_dir.name)

    return namespace_pkgs

# ============================================================
# 8. src/ 布局检测
# ============================================================
def _find_src_layout_packages(root: Path) -> Set[str]:
    """检测 src/ 布局下的包"""
    src_pkgs = set()
    src_dir = root / "src"
    if not src_dir.exists() or not src_dir.is_dir():
        return src_pkgs

    for entry in src_dir.iterdir():
        if not entry.is_dir() or entry.name.startswith('.'):
            continue
        if (entry / "__init__.py").exists():
            src_pkgs.add(entry.name)
        elif any(f.suffix == '.py' for f in entry.iterdir() if f.is_file()):
            src_pkgs.add(entry.name)

    return src_pkgs

# ============================================================
# 9. Django 项目检测
# ============================================================
def _detect_django_project(root: Path) -> Set[str]:
    """检测 Django 项目结构"""
    django_internal = set()
    if not (root / "manage.py").exists():
        return django_internal

    for entry in root.iterdir():
        if not entry.is_dir() or entry.name.startswith('.'):
            continue
        if any((entry / f).exists() for f in ["models.py", "views.py", "urls.py", "apps.py"]):
            django_internal.add(entry.name)

    settings_dir = root / "settings"
    if settings_dir.exists() and settings_dir.is_dir():
        django_internal.add("settings")

    return django_internal

# ============================================================
# 10. pyproject.toml 解析
# ============================================================
def _parse_pyproject_for_local_modules(root: Path) -> Set[str]:
    """解析 pyproject.toml，提取项目定义的内部模块名"""
    if tomllib is None:
        return set()

    toml_path = root / "pyproject.toml"
    if not toml_path.exists():
        return set()

    modules = set()
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

        proj = data.get("project", {})
        proj_name = proj.get("name")
        if proj_name:
            modules.add(proj_name.replace("-", "_").replace(".", "_"))

        setuptools = data.get("tool", {}).get("setuptools", {})
        packages_cfg = setuptools.get("packages", {})
        if isinstance(packages_cfg, dict):
            find_cfg = packages_cfg.get("find", {})
            where_dirs = find_cfg.get("where", [])
            for where in where_dirs:
                where_path = root / where
                if where_path.exists():
                    for pkg_dir in where_path.iterdir():
                        if pkg_dir.is_dir() and not pkg_dir.name.startswith('.'):
                            if (pkg_dir / "__init__.py").exists() or \
                               any(f.suffix == '.py' for f in pkg_dir.iterdir() if f.is_file()):
                                modules.add(pkg_dir.name)

        py_modules = setuptools.get("py_modules", [])
        if isinstance(py_modules, list):
            modules.update(py_modules)

        poetry = data.get("tool", {}).get("poetry", {})
        poetry_packages = poetry.get("packages", [])
        if isinstance(poetry_packages, list):
            for pkg in poetry_packages:
                if isinstance(pkg, dict):
                    include = pkg.get("include", "")
                    from_dir = pkg.get("from", "")
                    if include:
                        modules.add(include)
                    if from_dir:
                        where_path = root / from_dir
                        if where_path.exists():
                            for pkg_dir in where_path.iterdir():
                                if pkg_dir.is_dir() and not pkg_dir.name.startswith('.'):
                                    modules.add(pkg_dir.name)
                elif isinstance(pkg, str):
                    modules.add(pkg)

        scripts = proj.get("scripts", {})
        for script_cmd in scripts.values():
            if ":" in script_cmd:
                module_path = script_cmd.split(":")[0]
                top_module = module_path.split(".")[0]
                modules.add(top_module)

    except Exception as e:
        print(f"⚠️ 解析 pyproject.toml 警告: {e}")

    return modules

# ============================================================
# 11. requirements.txt 解析
# ============================================================
def _parse_requirements_txt(file_path: Path) -> Set[str]:
    packages = set()
    if not file_path.exists():
        return packages
    try:
        for line in file_path.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('-'):
                continue
            pkg = line.split(';')[0].split('@')[0].split('#')[0].split()[0]
            for sep in ('==', '>=', '<=', '~=', '!=', '>', '<'):
                if sep in pkg:
                    pkg = pkg.split(sep)[0]
                    break
            pkg = pkg.strip()
            if pkg:
                packages.add(pkg)
    except Exception:
        pass
    return packages

# ============================================================
# 12. pyproject 依赖提取
# ============================================================
def _parse_pyproject_deps(file_path: Path) -> Set[str]:
    packages = set()
    if not file_path.exists():
        return packages
    try:
        with open(file_path, "rb") as f:
            data = tomllib.load(f)

        deps = data.get("project", {}).get("dependencies", [])
        if isinstance(deps, list):
            for dep in deps:
                pkg = dep.split(';')[0].split('@')[0].split()[0]
                for sep in ('==', '>=', '<=', '~=', '!=', '>', '<'):
                    if sep in pkg:
                        pkg = pkg.split(sep)[0]
                        break
                if pkg:
                    packages.add(pkg)

        optional = data.get("project", {}).get("optional-dependencies", {})
        if isinstance(optional, dict):
            for group_deps in optional.values():
                if isinstance(group_deps, list):
                    for dep in group_deps:
                        pkg = dep.split(';')[0].split()[0]
                        for sep in ('==', '>=', '<=', '~=', '!=', '>', '<'):
                            if sep in pkg:
                                pkg = pkg.split(sep)[0]
                                break
                        if pkg:
                            packages.add(pkg)
    except Exception:
        pass
    return packages

# ============================================================
# 13. 综合构建内部模块白名单
# ============================================================
def _build_internal_modules(root: Path) -> Set[str]:
    """综合构建内部模块白名单，支持多种项目结构"""
    internal = set()

    packages = _find_package_roots(root)
    internal.update(_collect_modules_from_packages(root, packages))

    internal.update(_find_namespace_packages(root))

    internal.update(_find_src_layout_packages(root))

    for py_file in _walk_python_files(root):
        if py_file.name == "__init__.py":
            continue
        try:
            rel = py_file.relative_to(root)
            parts = rel.with_suffix("").parts
            for i in range(len(parts)):
                internal.add(".".join(parts[:i+1]))
            for part in parts:
                internal.add(part)
        except ValueError:
            internal.add(py_file.stem)

    internal.update(_parse_pyproject_for_local_modules(root))

    internal.update(_detect_django_project(root))

    for name in COMMON_INTERNAL_NAMES:
        if (root / name).is_dir() or (root / f"{name}.py").exists():
            internal.add(name)

    return internal

# ============================================================
# 14. 标准库获取
# ============================================================
def _get_stdlib_modules() -> Set[str]:
    """获取 Python 标准库模块列表"""
    if hasattr(sys, 'stdlib_module_names'):
        return set(sys.stdlib_module_names)

    try:
        import pkgutil
        return {m.name for m in pkgutil.iter_modules() if m.module_finder is None}
    except Exception:
        return {
            "abc", "argparse", "array", "ast", "asyncio", "atexit", "base64",
            "binascii", "bisect", "builtins", "bz2", "calendar", "cgi",
            "cgitb", "chunk", "cmath", "cmd", "code", "codecs", "codeop",
            "collections", "colorsys", "compileall", "concurrent", "configparser",
            "contextlib", "contextvars", "copy", "copyreg", "cProfile", "crypt",
            "csv", "ctypes", "curses", "dataclasses", "datetime", "dbm",
            "decimal", "difflib", "dis", "distutils", "doctest", "email",
            "encodings", "enum", "errno", "faulthandler", "fcntl", "filecmp",
            "fileinput", "fnmatch", "fractions", "ftplib", "functools", "gc",
            "getopt", "getpass", "gettext", "glob", "graphlib", "grp", "gzip",
            "hashlib", "heapq", "hmac", "html", "http", "idlelib", "imaplib",
            "imghdr", "imp", "importlib", "inspect", "io", "ipaddress",
            "itertools", "json", "keyword", "lib2to3", "linecache", "locale",
            "logging", "lzma", "mailbox", "mailcap", "marshal", "math",
            "mimetypes", "mmap", "modulefinder", "multiprocessing", "netrc",
            "nis", "nntplib", "numbers", "operator", "optparse", "os",
            "ossaudiodev", "pathlib", "pdb", "pickle", "pickletools", "pipes",
            "pkgutil", "platform", "plistlib", "poplib", "posix", "posixpath",
            "pprint", "profile", "pstats", "pty", "pwd", "py_compile",
            "pyclbr", "pydoc", "queue", "quopri", "random", "re",
            "readline", "reprlib", "resource", "rlcompleter", "runpy",
            "sched", "secrets", "select", "selectors", "shelve", "shlex",
            "shutil", "signal", "site", "smtpd", "smtplib", "sndhdr",
            "socket", "socketserver", "spwd", "sqlite3", "ssl", "stat",
            "statistics", "string", "stringprep", "struct", "subprocess",
            "sunau", "symtable", "sys", "sysconfig", "syslog", "tabnanny",
            "tarfile", "telnetlib", "tempfile", "termios", "test",
            "textwrap", "threading", "time", "timeit", "tkinter", "token",
            "tokenize", "trace", "traceback", "tracemalloc", "tty", "turtle",
            "turtledemo", "types", "typing", "unicodedata", "unittest",
            "urllib", "uu", "uuid", "venv", "warnings", "wave", "weakref",
            "webbrowser", "winreg", "winsound", "wsgiref", "xdrlib", "xml",
            "xmlrpc", "zipapp", "zipfile", "zipimport", "zlib", "__future__",
        }

# ============================================================
# 15. AST 扫描器
# ============================================================
class ImportScanner(ast.NodeVisitor):
    def __init__(self):
        self.imports: Set[str] = set()
        self.dynamic_warnings: List[str] = []
        self.current_file: str = ""

    def visit_Import(self, node):
        for alias in node.names:
            self.imports.add(alias.name.split('.')[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.level == 0 and node.module:
            self.imports.add(node.module.split('.')[0])
        self.generic_visit(node)

    def visit_Call(self, node):
        func_name = None
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        if func_name in ('__import__', 'import_module'):
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                self.imports.add(node.args[0].value.split('.')[0])
            else:
                self.dynamic_warnings.append(
                    f"{self.current_file}:{node.lineno} - 无法静态推断的动态导入"
                )
        self.generic_visit(node)

# ============================================================
# 16. 主扫描工具
# ============================================================
@tool
def scan_python_imports(project_path: str) -> Dict[str, Any]:
    """
    扫描 Python 项目，提取第三方依赖。
    支持：标准包、src/布局、命名空间包、单文件脚本、Django、monorepo。
    """
    print(f"\n📊 [扫描工具] 开始扫描项目: {project_path}")

    project_path = convert_wsl_to_windows(project_path)
    root_dir = Path(project_path).resolve()

    if not root_dir.exists() or not root_dir.is_dir():
        print(f"❌ [扫描工具] 路径不存在或非目录 -> {root_dir}")
        return {"error": f"路径不存在或非目录: {project_path}"}

    # Step 1: 构建内部模块白名单
    internal_modules = _build_internal_modules(root_dir)
    print(f"🛡️ [扫描工具] 检测到 {len(internal_modules)} 个内部模块")
    if len(internal_modules) <= 20:
        print(f"   内部模块: {sorted(internal_modules)}")

    # Step 2: 获取标准库
    stdlib_modules = _get_stdlib_modules()

    # Step 3: AST 扫描
    scanner = ImportScanner()
    total_py_files = 0
    failed_files = 0

    for py_file in _walk_python_files(root_dir):
        total_py_files += 1
        try:
            scanner.current_file = str(py_file.relative_to(root_dir))
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
            scanner.visit(tree)
        except Exception as e:
            failed_files += 1
            scanner.dynamic_warnings.append(f"{py_file.name} - 解析异常: {str(e)}")

    print(f"📦 [扫描工具] 扫描了 {total_py_files} 个 .py 文件，{failed_files} 个失败")
    print(f"📦 [扫描工具] 原始导入项: {len(scanner.imports)} 个")

    # Step 4: 补充配置文件依赖
    config_packages = _parse_requirements_txt(root_dir / "requirements.txt") | \
                      _parse_pyproject_deps(root_dir / "pyproject.toml")
    if config_packages:
        print(f"📄 [扫描工具] 配置文件补充: {len(config_packages)} 个包")
        scanner.imports.update(config_packages)

    # Step 5: 过滤（大小写不敏感 + 内置映射去重）
    third_party_imports: Set[str] = set()
    shadowing_warnings: List[str] = []

    internal_lower = {m.lower() for m in internal_modules}
    stdlib_lower = {s.lower() for s in stdlib_modules}

    for pkg in scanner.imports:
        pkg_lower = pkg.lower()

        # 1. 匹配内部模块（大小写不敏感）
        if pkg_lower in internal_lower:
            if pkg_lower in stdlib_lower:
                shadowing_warnings.append(f"命名冲突: '{pkg}' 同时是内部模块和标准库")
            continue

        # 2. 匹配标准库（大小写不敏感）
        if pkg_lower in stdlib_lower or pkg == '__future__':
            continue

        # 3. 内置映射（PIL → Pillow 等）
        mapped_pkg = BUILTIN_MAPPING.get(pkg, BUILTIN_MAPPING.get(pkg_lower, pkg))
        mapped_lower = mapped_pkg.lower()

        # 4. 映射后再次检查内部模块
        if mapped_lower in internal_lower:
            continue

        third_party_imports.add(mapped_pkg)

    result_imports = sorted(third_party_imports, key=str.lower)

    print(f"🎯 [扫描工具] 最终第三方依赖: {len(result_imports)} 个")
    if result_imports:
        print(f"   {result_imports[:20]}{'...' if len(result_imports) > 20 else ''}")

    return {
        "third_party_imports": result_imports,
        "internal_modules_detected": sorted(internal_modules, key=str.lower),
        "warnings": sorted(set(scanner.dynamic_warnings + shadowing_warnings)),
        "stats": {
            "total_py_files_scanned": total_py_files,
            "total_raw_imports_extracted": len(scanner.imports),
            "files_failed_to_parse": failed_files,
            "internal_modules_count": len(internal_modules),
            "third_party_count": len(result_imports),
        }
    }