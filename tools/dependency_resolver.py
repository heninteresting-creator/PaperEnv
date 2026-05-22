"""
依赖解析工具（裸包名版）
功能：只映射包名、检查 PyPI 存在性，不获取版本号。
"""
import time
import os
import json
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple, Any
from langchain_core.tools import tool


# ==========================================
# 内置映射表（导入名 → PyPI 规范名）
# ==========================================
BUILTIN_MAPPING: Dict[str, str] = {
    "PIL": "Pillow",
    "pil": "Pillow",
    "Image": "Pillow",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "cv2": "opencv-python",
    "bs4": "beautifulsoup4",
    "yaml": "PyYAML",
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
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "pytz": "pytz",
    "requests": "requests",
    "urllib3": "urllib3",
    "click": "click",
    "jinja2": "Jinja2",
    "markdown": "Markdown",
    "psycopg2": "psycopg2-binary",
    "redis": "redis",
    "celery": "celery",
    "pytest": "pytest",
    "sphinx": "Sphinx",
    "twisted": "Twisted",
    "lxml": "lxml",
    "html5lib": "html5lib",
    "openpyxl": "openpyxl",
    "xlrd": "xlrd",
    "xlsxwriter": "XlsxWriter",
    # === 新增 ===
    "googleapiclient": "google-api-python-client",
}

# ==========================================
# 重依赖包识别表（需系统级编译 / 特殊安装）
# Scanner 识别后剔除，Installer 跳过，提示用户手动处理
# ==========================================
### 新增 ↓↓↓
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
    "sam-2": {
        "category": "unresolvable",
        "reason": "同 lang-sam",
        "fix_hint": "同 lang-sam",
    },
}

def _is_heavy_package(pypi_name: str) -> Optional[Dict[str, Any]]:
    """检查包名是否在重依赖表中，返回元数据或 None"""
    bare = pypi_name.lower().replace("_", "-").replace(".", "-")
    return HEAVY_PACKAGES.get(bare)
### 新增 ↑↑↑

# 内存缓存：key = 查询字符串，value = (规范包名, 是否存在)
_CACHE: Dict[str, Tuple[str, bool]] = {}


def _fetch_pypi_info(package_name: str, timeout: int = 10) -> Tuple[str, bool]:
    """
    查询 PyPI 确认包名存在性（不获取版本号）。
    返回: (规范包名, 是否存在)
    """
    url = f"https://pypi.org/pypi/{package_name}/json"
    
    for attempt in range(2):  # 2 次尝试（原 1 次 + 1 次重试）
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Agent-Dependency-Resolver"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
                info = data.get("info", {})
                normalized_name = info.get("name", package_name)
                return normalized_name, True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return package_name, False
            # 非 404 也重试（502/503 抖动）
            print(f"   [DEBUG PyPI] {package_name} HTTP {e.code} (attempt {attempt+1})")
        except Exception as e:
            print(f"   [DEBUG PyPI] {package_name} {type(e).__name__}: {e} (attempt {attempt+1})")
        
        if attempt == 0:
            time.sleep(0.5)
    
    return package_name, False


def _resolve_single(pkg: str) -> Dict:
    """
    解析单个导入名，返回裸包名（无版本号）。
    """
    # 1. 检查缓存
    if pkg in _CACHE:
        norm_name, exists = _CACHE[pkg]
    else:
        # 2. 应用内置映射
        if pkg in BUILTIN_MAPPING:
            candidate = BUILTIN_MAPPING[pkg]
            norm_name, exists = _fetch_pypi_info(candidate)
            if exists:
                _CACHE[pkg] = (norm_name, True)
            else:
                # 映射表可能过时，回退查询原始名
                norm_name, exists = _fetch_pypi_info(pkg)
                _CACHE[pkg] = (norm_name, exists)
        else:
            norm_name, exists = _fetch_pypi_info(pkg)
            _CACHE[pkg] = (norm_name, exists)

    # 3. 返回裸包名（无版本号）
    return {
        "original_name": pkg,
        "pypi_name": norm_name,
        "version": None,           # ← 强制 None，裸包名
        "source": "unversioned",   # ← 标记来源
        "exists_on_pypi": exists,
    }


@tool
def resolve_dependencies(package_names: List[str]) -> str:
    """
    并发解析依赖：映射包名并检查 PyPI 存在性，不获取版本号。

    参数:
        package_names: 原始导入名列表（例如 ["PIL", "requests", "unknown_lib"]）

    返回:
        JSON 格式字符串，包含:
            - resolved: 解析成功的裸包名列表
            - normal_packages: 可自动安装的普通包（新增）
            - heavy_packages: 需手动处理的系统级依赖包（新增）
            - failed: 无法解析的包名列表（PyPI 上不存在）
            - warnings: 警告信息
            - stats: 统计信息
    """
    print(f"\n📦 [依赖解析工具] 解析依赖: {package_names}")
    resolved = []
    failed = []
    warnings = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_pkg = {executor.submit(_resolve_single, pkg): pkg for pkg in package_names}
        for future in as_completed(future_to_pkg):
            pkg = future_to_pkg[future]
            try:
                result = future.result()
                if result["exists_on_pypi"]:
                    resolved.append(result)
                    if result["original_name"] != result["pypi_name"]:
                        warnings.append(f"已映射: '{result['original_name']}' → '{result['pypi_name']}'")
                else:
                    failed.append(pkg)
                    warnings.append(f"无法解析: '{pkg}'（PyPI 不存在）")
            except Exception as e:
                failed.append(pkg)
                warnings.append(f"解析 '{pkg}' 时出错: {str(e)}")

    ### 修改 ↓↓↓
    # 分流：普通包 vs 重依赖包
    normal_packages = []
    heavy_packages = []
    for r in resolved:
        meta = _is_heavy_package(r["pypi_name"])
        if meta:
            heavy_packages.append({**r, "meta": meta})
            warnings.append(f"重依赖包（需手动处理）: '{r['pypi_name']}' —— {meta['reason']}")
        else:
            normal_packages.append(r)

    output = {
        "resolved": resolved,                  # 保持兼容：所有解析成功的包
        "normal_packages": normal_packages,    # 新增：可自动安装的包
        "heavy_packages": heavy_packages,      # 新增：需手动处理的包
        "failed": failed,
        "warnings": warnings,
        "stats": {
            "total": len(package_names),
            "resolved_count": len(resolved),
            "failed_count": len(failed),
            "heavy_count": len(heavy_packages),  # 新增统计
        }
    }
    ### 修改 ↑↑↑
    
    return json.dumps(output, ensure_ascii=False, indent=2)