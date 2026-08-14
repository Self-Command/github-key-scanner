# -*- coding: utf-8 -*-
"""配置模块：从 .env / 环境变量加载项目配置（零第三方依赖的 dotenv 解析）。"""
import os
from pathlib import Path

# 项目根目录（本文件所在目录）
BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path | None = None) -> None:
    """解析 .env 文件（支持 KEY=VALUE、# 注释、引号包裹），写入进程环境变量。"""
    env_file = path or (BASE_DIR / ".env")
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Config:
    """集中管理全部可配置项。"""

    # ---- GitHub 搜索 ----
    GITHUB_TOKEN = _get("GITHUB_TOKEN")                     # 代码搜索必须的令牌
    PER_PAGE = int(_get("GITHUB_PER_PAGE", "100"))          # 每页结果数（代码搜索上限 100）
    MAX_PAGES = int(_get("GITHUB_MAX_PAGES", "5"))          # 每个语言查询最多抓取页数（配合限速，默认 5 页/语言）
    SORT = _get("GITHUB_SORT", "indexed")                   # 排序：indexed=最近被索引
    MIN_INTERVAL = float(_get("GITHUB_MIN_INTERVAL", "6.5"))  # 代码搜索限速 10 次/分钟，间隔下限
    SEARCH_PATTERN = _get("SEARCH_PATTERN", "sk-")          # 搜索字面量（GitHub 端不做正则，本地严格过滤）
    SEARCH_GLOBAL_PAGES = int(_get("SEARCH_GLOBAL_PAGES", "1"))  # 全局兜底查询页数（覆盖 .env/.conf/.cfg 等无语言文件）
    FETCH_FILE_CONTENT = _get("FETCH_FILE_CONTENT", "true").lower() == "true"  # 是否拉取文件全文

    # ---- AI 分析（OpenAI 兼容接口，可填任意兼容服务）----
    AI_ENABLED = _get("AI_ENABLED", "true").lower() == "true"
    AI_BASE_URL = _get("AI_BASE_URL", "https://api.deepseek.com/v1")  # 默认 DeepSeek 兼容端点
    AI_API_KEY = _get("AI_API_KEY")
    AI_MODEL = _get("AI_MODEL", "deepseek-chat")
    AI_TIMEOUT = int(_get("AI_TIMEOUT", "60"))
    AI_TEMPERATURE = float(_get("AI_TEMPERATURE", "0.05"))  # 分析温度：越低越稳定保守（默认 0.05）
    AI_MAX_FILES = int(_get("AI_MAX_FILES", "200"))         # 每轮扫描最多交给 AI 分析的文件数
    AI_MAX_TOKENS = int(_get("AI_MAX_TOKENS", "2000"))      # 单文件提交给 AI 的 token 上限（超出则截取 key 上下文）
    KEY_CONTEXT_CHARS = int(_get("KEY_CONTEXT_CHARS", "1200"))  # 截取时每个 key 前后保留的字符数
    AUTO_TEST_LLM = _get("AUTO_TEST_LLM", "true").lower() == "true"  # 新增大模型密钥入库后自动 /v1/models 测试

    # ---- 数据库与 Web ----
    DB_PATH = BASE_DIR / _get("DB_PATH", "data/keys.db")
    WEB_HOST = _get("WEB_HOST", "127.0.0.1")
    WEB_PORT = int(_get("WEB_PORT", "8765"))

    # ---- 全自动托管（daemon 调度与增量扫描）----
    SCAN_INTERVAL = int(_get("SCAN_INTERVAL", "7200"))       # daemon 两轮扫描间隔（秒），默认 2 小时
    SCAN_INCREMENTAL = _get("SCAN_INCREMENTAL", "true").lower() == "true"  # 增量：只扫上次成功后新索引文件
    TRIGGER_FILE = BASE_DIR / _get("SCAN_TRIGGER_FILE", "data/scan.trigger")  # Web『立即扫描』触发文件
    STALE_TASK_SECONDS = int(_get(
        "STALE_TASK_SECONDS", str(max(14_400, SCAN_INTERVAL * 2))))
    HEALTH_MAX_SCAN_AGE = int(_get(
        "HEALTH_MAX_SCAN_AGE", str(max(18_000, SCAN_INTERVAL * 2 + 900))))
    # ---- 搜索范围（代码文件 + 常见配置文件）----
    # py/json/js/ts 代码文件；toml/yaml/yml/ini/conf/cfg/env 配置文件
    DEFAULT_EXTENSIONS = ["py", "json", "js", "ts",
                          "toml", "yaml", "yml", "ini", "conf", "cfg", "env"]

    @property
    def ai_ready(self) -> bool:
        """AI 可用 = 开关开启 + 已配置密钥。"""
        return self.AI_ENABLED and bool(self.AI_API_KEY) and bool(self.AI_BASE_URL)


config = Config()
