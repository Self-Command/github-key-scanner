# -*- coding: utf-8 -*-
"""单元测试：正则提取、端点匹配、规则引擎、数据库读写（无外部依赖，直接 python 运行）。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gks.analyzer import (AIAnalyzer, RuleAnalyzer, classify_key, estimate_tokens,
                          preflight, truncate_context)
from gks.database import KeyDatabase
from gks.endpoints import match_by_domain, normalize_base_url, to_markdown_table
from gks.github_search import GitHubCodeSearch, Hit, extract_keys
from gks.key_tester import pick_base, test_key

FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  [通过] {name}")
    else:
        print(f"  [失败] {name} {detail}")
        FAIL.append(name)


def test_extract():
    print("== extract_keys ==")
    text = ('key1="sk-\u0030123456789abcdefghijklmnopqrstuv"; '
            'bad1="sk-XX3456789abcdef0123456789abcdef" '       # 含大写，不匹配
            'bad2="sk-0123456789abcdef0123456789abcde" '       # 31 位，不匹配
            'bad3="xsk-\u0030123456789abcdefghijklmnopqrstuv" '     # 前缀不同，不匹配
            'dup="sk-\u0030123456789abcdefghijklmnopqrstuv"')       # 重复，应去重
    keys = extract_keys(text)
    check("提取到 1 个有效密钥", len(keys) == 1, str(keys))
    check("密钥去重保序", keys == ["sk-\u0030123456789abcdefghijklmnopqrstuv"])

    # 多类型密钥混合提取（OpenAI 项目 / Anthropic / Groq / Tavily / ElevenLabs / AWS / GitHub）
    multi = (
        'OPENAI_KEY="sk-proj-abcdefghijklmnopqrstuvwxyzABCDEF012345"\n'
        'ANT="sk-ant-api03-abcdefghijklmnopqrstuvwxyzABCDEF0123456789"\n'
        'GROQ="gsk_abcdefghijklmnopqrstuvwxyzABCDEF012345"\n'
        'TAVILY="tvly-abcdefghijklmnopqrstuvwxyzABCDEF012345"\n'
        'ELEVEN="sk_abcdefghijklmnopqrstuvwxyzABCDEF012345"\n'
        'AWS="AKIAIOSFODNN7EXAMPLE"\n'
        'GH="ghp_abcdefghijklmnopqrstuvwxyzABCDEF0123456789"\n'
        'STD="sk-\u0030123456789abcdefghijklmnopqrstuv"'
    )
    keys_m = extract_keys(multi)
    check("提取 8 个不同类型密钥", len(keys_m) == 8, str(keys_m))
    check("含 sk-proj- 变体", any(k.startswith("sk-proj-") for k in keys_m))
    check("含 gsk_ Groq", any(k.startswith("gsk_") for k in keys_m))
    check("含 tvly- Tavily", any(k.startswith("tvly-") for k in keys_m))
    check("含 sk_ ElevenLabs", any(k.startswith("sk_") for k in keys_m))
    check("含 AKIA AWS", any(k.startswith("AKIA") for k in keys_m))
    check("含 ghp_ GitHub", any(k.startswith("ghp_") for k in keys_m))


def test_endpoints():
    print("== endpoints ==")
    table = to_markdown_table()
    check("端点库 ≥ 50 条", len(table.splitlines()) >= 51, f"共 {len(table.splitlines())} 行")
    for must in ["DeepSeek", "智谱 GLM", "Kimi 月之暗面", "OpenAI", "xAI Grok",
                 "通义千问 Qwen", "豆包/火山方舟", "OpenRouter", "硅基流动 SiliconFlow",
                 "AnyRouter", "OpenCode Zen", "OpenCode Go"]:
        check(f"包含 {must}", must in table)
    hits = match_by_domain("https://api.deepseek.com/v1/chat/completions")
    check("域名匹配 DeepSeek", hits and hits[0]["provider"] == "DeepSeek", str(hits))
    hits = match_by_domain("use base_url https://open.bigmodel.cn/api/paas/v4 glm-4.6")
    check("域名匹配智谱 GLM", hits and "GLM" in hits[0]["provider"], str(hits))
    # OpenCode Go 域名更长，应优先于 OpenCode Zen
    hits = match_by_domain("base https://opencode.ai/zen/go/v1 model opencode-go/kimi-k3")
    check("OpenCode Go 优先于 Zen", hits and hits[0]["provider"] == "OpenCode Go", str(hits))
    hits = match_by_domain("base https://anyrouter.dev/api/v1")
    check("域名匹配 AnyRouter", hits and hits[0]["provider"] == "AnyRouter", str(hits))


def test_normalize_base():
    print("== normalize_base ==")
    norm_cases = [
        ("https://api.openai.com", "https://api.openai.com/v1"),
        ("https://api.deepseek.com", "https://api.deepseek.com/v1"),
        ("https://open.bigmodel.cn", "https://open.bigmodel.cn/api/paas/v4"),
        ("https://dashscope.aliyuncs.com",
         "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        ("https://ark.cn-beijing.volces.com",
         "https://ark.cn-beijing.volces.com/api/v3"),
        ("https://api.perplexity.ai", "https://api.perplexity.ai"),
        ("https://api.openai.com/v1/chat/completions",
         "https://api.openai.com/v1"),
        ("https://api.anthropic.com/v1/messages", "https://api.anthropic.com/v1"),
        ("https://my-gateway.xyz", "https://my-gateway.xyz/v1"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
        ("", ""),
    ]
    for inp, exp in norm_cases:
        got = normalize_base_url(inp)
        check(f"normalize {inp or '(空)'} → {exp}", got == exp, f"实际 {got}")
    # 私有网关补 /v1；Provider 名不影响结果（域名优先）
    check("私有网关补 /v1", normalize_base_url("http://localhost:8000") == "http://localhost:8000/v1",
          normalize_base_url("http://localhost:8000"))
    check("v1beta 保留", normalize_base_url("https://generativelanguage.googleapis.com/v1beta")
          == "https://generativelanguage.googleapis.com/v1beta",
          normalize_base_url("https://generativelanguage.googleapis.com/v1beta"))


def test_preflight():
    print("== preflight ==")
    text = ('DEEPSEEK_API_KEY = "sk-\u0030123456789abcdefghijklmnopqrstuv"\n'
            'base_url = "https://api.deepseek.com/v1"\n'
            "model = \"deepseek-chat\"")
    pf = preflight(text)
    check("关键词命中 deepseek", "deepseek" in pf["keywords"], str(pf["keywords"]))
    check("候选含 DeepSeek", "DeepSeek" in pf["providers"], str(pf["providers"]))
    check("提取文件内 URL", "https://api.deepseek.com/v1" in pf["urls"], str(pf["urls"]))
    check("识别 base_url 赋值", "https://api.deepseek.com/v1" in pf["base_urls"], str(pf["base_urls"]))
    check("唯一候选给出 suggested", pf["suggested"] == "DeepSeek", str(pf))
    pf2 = preflight('ANTHROPIC_BASE_URL="https://anyrouter.dev/api" key="sk-abc"')
    check("AnyRouter 关键词与 URL", "anyrouter" in pf2["keywords"]
          and "https://anyrouter.dev/api" in pf2["urls"], str(pf2))


def test_truncate():
    print("== truncate / token 估算 ==")
    key = "sk-\u0030123456789abcdefghijklmnopqrstuv"
    short = f"a = \"{key}\""
    check("短文本原样返回", truncate_context(short, [key]) == short)
    long_text = ("# 项目配置\n" + "padding_line = " + "x" * 4000 + "\n" * 40) * 6  # 约 2.5 万字符 ≈ 6000+ token
    mid = len(long_text) // 2
    long_text = long_text[:mid] + f"\napi_key = \"{key}\"\nbase_url = \"https://api.deepseek.com/v1\"\n" + long_text[mid:]
    out = truncate_context(long_text, [key], max_tokens=2000, window=1200)
    check("截断后仍含密钥", key in out)
    check("截断后含 base_url 线索", "api.deepseek.com" in out)
    check("截断后 token 不超限", estimate_tokens(out) <= 2000, f"{estimate_tokens(out)} tokens")
    check("截断后长度大幅缩小", len(out) < len(long_text) // 5, f"{len(out)} vs {len(long_text)}")
    # 两个相距很远的密钥 → 中间应有省略标记
    k2 = "sk-\u0031234567890abcdefghijklmnopqrstuv"
    long3 = key + "p" * 40000 + k2
    out3 = truncate_context(long3, [key, k2], max_tokens=2000, window=1200)
    check("分散密钥截断带省略标记", "…" in out3 and key in out3 and k2 in out3)
    long2 = "y" * 50000 + key + "z" * 50000
    out2 = truncate_context(long2, [key], max_tokens=2000, window=1200)
    check("无线索超长文件也截断", key in out2 and estimate_tokens(out2) <= 2000)


def test_rules():
    print("== RuleAnalyzer ==")
    hit = Hit(repo="a/b", path="c.py", html_url="https://github.com/a/b/blob/main/c.py",
              language="Python",
              fragments=[], full_text='DEEPSEEK_API_KEY = "sk-\u0030123456789abcdefghijklmnopqrstuv"\nBASE="https://api.deepseek.com"')
    recs = RuleAnalyzer().analyze(hit)
    check("规则引擎识别 DeepSeek", recs[0]["provider"] == "DeepSeek", str(recs))
    check("规则引擎给出 base", recs[0]["api_base"] == "https://api.deepseek.com", str(recs))
    check("LLM 分类为 llm", recs[0]["category"] == "llm", str(recs))

    # 文件显式给出 /v1 端点 → 规则引擎应优先采用文件中的 URL，而非端点库默认值
    hit1b = Hit(repo="a/b", path="c.py", html_url="u", language="Python",
                fragments=[],
                full_text='key="sk-\u0037123456789abcdefghijklmnopqrstuv"\nBASE_URL="https://api.deepseek.com/v1"')
    recs1b = RuleAnalyzer().analyze(hit1b)
    check("文件 URL 优先于端点库默认", recs1b[0]["api_base"] == "https://api.deepseek.com/v1", str(recs1b))
    check("文件 URL 指定高置信度", recs1b[0]["confidence"] >= 0.9, str(recs1b))

    # 关键词启发式（无域名时）
    hit2 = Hit(repo="a/b", path="c.ts", html_url="u", language="TypeScript",
               fragments=[],
               full_text="const K = \"sk-\u0038123456789abcdefghijklmnopqrstuv\"; // kimi moonshot key")
    recs2 = RuleAnalyzer().analyze(hit2)
    check("关键词命中 Kimi", recs2[0]["provider"] == "Kimi 月之暗面", str(recs2))

    hit3 = Hit(repo="a/b", path="c.ts", html_url="u", language="TypeScript",
               fragments=[], full_text="const K = \"sk-\u0038123456789abcdefghijklmnopqrstuv\";")
    recs3 = RuleAnalyzer().analyze(hit3)
    check("无上下文判 unknown", recs3[0]["provider"] == "unknown", str(recs3))
    check("无上下文低置信度", recs3[0]["confidence"] <= 0.3, str(recs3))
    check("无上下文分类 unknown", recs3[0]["category"] == "unknown", str(recs3))

    # 多密钥同文件：每一条独立返回（规则引擎也逐条判断）
    multi_text = (
        'DEEPSEEK_API_KEY="sk-\u0030123456789abcdefghijklmnopqrstuv"\n'
        'ELEVENLABS_API_KEY="sk_abcdefghijklmnopqrstuvwxyzABCDEF012345"\n'
        'TAVILY_API_KEY="tvly-abcdefghijklmnopqrstuvwxyzABCDEF012345"\n'
        'AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE"'
    )
    hitm = Hit(repo="a/b", path="multi.py", html_url="u", language="Python",
               fragments=[], full_text=multi_text)
    recsm = RuleAnalyzer().analyze(hitm)
    check("多密钥逐条独立返回 4 条", len(recsm) == 4, str([r["api_key"] for r in recsm]))
    by_key = {r["api_key"]: r for r in recsm}
    check("DeepSeek 分类 llm", by_key["sk-\u0030123456789abcdefghijklmnopqrstuv"]["category"] == "llm")
    check("ElevenLabs 分类 other", by_key["sk_abcdefghijklmnopqrstuvwxyzABCDEF012345"]["category"] == "other")
    check("Tavily 分类 other", by_key["tvly-abcdefghijklmnopqrstuvwxyzABCDEF012345"]["category"] == "other")
    check("AWS 分类 cloud", by_key["AKIAIOSFODNN7EXAMPLE"]["category"] == "cloud")


def test_ai_parse():
    print("== AIAnalyzer._parse（多 Key 独立解析）==")
    # AI 回复多条 → 每条独立成为一个 dict，绝不合并
    ai_out = '''{
      "keys": [
        {"api_key":"sk-\u0030123456789abcdefghijklmnopqrstuv","provider":"DeepSeek",
         "api_base":"https://api.deepseek.com","confidence":0.95,
         "notes":"DEEPSEEK_API_KEY","category":"llm"},
        {"api_key":"tvly-abcdefghijklmnopqrstuvwxyzABCDEF012345","provider":"Tavily",
         "api_base":"","confidence":0.8,
         "notes":"TAVILY_API_KEY 变量名","category":"other"},
        {"api_key":"AKIAIOSFODNN7EXAMPLE","provider":"AWS",
         "api_base":"https://s3.amazonaws.com","confidence":0.9,
         "notes":"AWS Access Key","category":"cloud"},
        {"api_key":"sk-\u0039999999999abcdefghijklmnopqrstuv","provider":"unknown",
         "api_base":"","confidence":0.2,
         "notes":"无法判断服务商","category":"unknown"}
      ]
    }'''
    parsed = AIAnalyzer._parse(ai_out)
    check("AI 回复 4 条 → 解析出 4 条", len(parsed) == 4, str(parsed))
    check("每条独立含完整字段", all({"api_key", "provider", "api_base", "confidence",
                                     "notes", "category"} <= set(r) for r in parsed))
    cats = {r["category"] for r in parsed}
    check("四类齐全 llm/other/cloud/unknown", cats == {"llm", "other", "cloud", "unknown"}, str(cats))
    check("Tavily 那条 provider 正确", parsed[1]["provider"] == "Tavily" and parsed[1]["category"] == "other")
    # AI 未返回 category → 按密钥格式兜底
    no_cat = '{"keys":[{"api_key":"tvly-abcdefghijklmnopqrstuvwxyzABCDEF012345","provider":"Tavily"}]}'
    parsed2 = AIAnalyzer._parse(no_cat)
    check("缺失 category 自动兜底 other", parsed2[0]["category"] == "other", str(parsed2))
    # Markdown 代码块包裹容忍
    wrapped = '```json\n{"keys":[{"api_key":"sk-\u0030123456789abcdefghijklmnopqrstuv","provider":"DeepSeek"}]}\n```'
    parsed3 = AIAnalyzer._parse(wrapped)
    check("容忍 ```json 代码块", len(parsed3) == 1 and parsed3[0]["provider"] == "DeepSeek", str(parsed3))
    # 5 分制置信度：整数 1~5 归一化为 0.2~1.0；旧格式 0~1 浮点保留
    five_scale = AIAnalyzer._parse('{"keys":[' +
        '{"api_key":"k1","provider":"A","confidence":5},' +
        '{"api_key":"k2","provider":"B","confidence":4},' +
        '{"api_key":"k3","provider":"C","confidence":2},' +
        '{"api_key":"k4","provider":"D","confidence":1}]}')
    confs = [r["confidence"] for r in five_scale]
    check("5 分制归一化 5→1.0/4→0.8/2→0.4/1→0.2",
          confs == [1.0, 0.8, 0.4, 0.2], str(confs))
    legacy = AIAnalyzer._parse('{"keys":[{"api_key":"k1","provider":"A","confidence":0.85}]}')
    check("旧格式 0.85 保留", legacy[0]["confidence"] == 0.85, str(legacy))
    none_c = AIAnalyzer._parse('{"keys":[{"api_key":"k1","provider":"A"}]}')
    check("缺省置信度给 1 分（0.2）", none_c[0]["confidence"] == 0.2, str(none_c))


def test_classify():
    print("== classify_key ==")
    check("AKIA → cloud", classify_key("AKIAIOSFODNN7EXAMPLE") == "cloud")
    check("tvly- → other", classify_key("tvly-abcdefghijklmnopqrstuvwxyzABCDEF012345") == "other")
    check("sk_ → other", classify_key("sk_abcdefghijklmnopqrstuvwxyzABCDEF012345") == "other")
    check("ghp_ → other", classify_key("ghp_abcdefghijklmnopqrstuvwxyzABCDEF0123456789") == "other")
    check("gsk_ → llm", classify_key("gsk_abcdefghijklmnopqrstuvwxyzABCDEF012345") == "llm")
    check("sk- 纯格式 → unknown", classify_key("sk-\u0030123456789abcdefghijklmnopqrstuv") == "unknown")
    check("上下文 azure → cloud", classify_key("sk-\u0030123456789abcdefghijklmnopqrstuv",
                                                "AZURE_SUBSCRIPTION_KEY=xxx") == "cloud")
    check("上下文 deepseek → llm", classify_key("sk-\u0030123456789abcdefghijklmnopqrstuv",
                                                "DEEPSEEK_API_KEY") == "llm")


def test_db():
    print("== KeyDatabase ==")
    with tempfile.TemporaryDirectory() as td:
        db = KeyDatabase(Path(td) / "t.db")
        rec = {"api_key": "sk-\u0030123456789abcdefghijklmnopqrstuv", "provider": "DeepSeek",
               "api_base": "https://api.deepseek.com", "confidence": 0.95,
               "category": "llm",
               "source_url": "https://github.com/a/b", "repo": "a/b", "file_path": "c.py",
               "language": "Python", "context": "ctx", "notes": "n"}
        check("首次插入", db.upsert(rec) is True)
        check("重复插入返回 False", db.upsert(rec) is False)
        check("计数为 1", db.count() == 1)
        rows, total = db.query(q="deepseek")
        check("关键词查询命中", total == 1 and rows[0]["provider"] == "DeepSeek")
        check("查询返回分类", rows[0]["category"] == "llm", str(rows[0].get("category")))
        check("状态更新", db.update_status(rows[0]["id"], "active"))
        check("统计字段", db.stats()["total"] == 1 and db.stats()["high_conf"] == 1)
        check("统计含分类分布", db.stats()["category"].get("llm") == 1, str(db.stats()["category"]))
        csv_text = db.export_csv()
        check("CSV 导出含 BOM 与表头", csv_text.startswith("\ufeffID") and "API_BASE_URL" in csv_text)
        check("CSV 含分类列", "分类" in csv_text.splitlines()[0])

        # 分类筛选
        rec2 = dict(rec, api_key="tvly-abcdefghijklmnopqrstuvwxyzABCDEF012345",
                    provider="Tavily", category="other", api_base="")
        db.upsert(rec2)
        rows, total = db.query(category="llm")
        check("分类筛选 llm 命中 1 条", total == 1 and rows[0]["api_key"].startswith("sk-"))
        rows, total = db.query(category="other")
        check("分类筛选 other 命中 1 条", total == 1 and rows[0]["provider"] == "Tavily")
        rows, total = db.query(category="cloud")
        check("分类筛选 cloud 命中 0 条", total == 0)
        check("导出带分类筛选", "Tavily" in db.export_csv(category="other"))

        # 旧库迁移：无 category 列的库自动补列
        import sqlite3
        old = Path(td) / "old.db"
        conn = sqlite3.connect(old)
        conn.execute("""CREATE TABLE api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT, api_key TEXT NOT NULL UNIQUE,
            provider TEXT DEFAULT 'unknown', api_base_url TEXT DEFAULT '',
            confidence REAL DEFAULT 0, source_url TEXT DEFAULT '', repo TEXT DEFAULT '',
            file_path TEXT DEFAULT '', language TEXT DEFAULT '', context TEXT DEFAULT '',
            notes TEXT DEFAULT '', status TEXT DEFAULT 'unknown',
            first_seen TEXT DEFAULT (datetime('now','localtime')),
            last_seen TEXT DEFAULT (datetime('now','localtime')))""")
        conn.commit()
        conn.execute("INSERT INTO api_keys (api_key, provider) VALUES (?, ?)",
                     ("sk-\u0061aaaaaaaaaaaaaaaghijklmnopqrstuv", "DeepSeek"))
        conn.commit()
        conn.close()
        db_old = KeyDatabase(old)  # 触发迁移
        rows, _ = db_old.query(category="unknown")
        check("旧库迁移后分类默认为 unknown", total_ok := (rows and rows[0]["category"] == "unknown"), str(rows[:1]))
        check("旧库迁移后可按分类查询", db_old.query(category="unknown")[1] == 1)


def test_tasks():
    print("== 扫描任务 / 增量游标 / 文件去重 ==")
    with tempfile.TemporaryDirectory() as td:
        db = KeyDatabase(Path(td) / "t.db")

        # 任务记录
        tid = db.start_task(trigger="schedule", mode="demo",
                            window_from="2026-08-01", window_to="2026-08-13", pages=3)
        check("start_task 返回 id", isinstance(tid, int) and tid > 0)
        db.finish_task(tid, status="success", hits=10, files_analyzed=8,
                       files_skipped=2, keys_found=12, keys_new=5, keys_updated=7)
        rows = db.recent_tasks(5)
        check("任务已记录", len(rows) == 1 and rows[0]["status"] == "success")
        check("任务统计完整", rows[0]["keys_new"] == 5 and rows[0]["keys_updated"] == 7
              and rows[0]["files_skipped"] == 2, str(rows[0]))
        check("任务耗时已计算", rows[0]["duration"] >= 0)

        # 成功任务更新游标
        cursor = db.get_cursor()
        check("游标窗口已推进", cursor["last_window_to"] == "2026-08-13", str(cursor))
        check("游标时间已更新", bool(cursor["last_scan_at"]))

        # 失败任务不推进游标
        tid2 = db.start_task(trigger="manual", mode="real",
                             window_from="2026-08-13", window_to="2026-08-14", pages=1)
        db.finish_task(tid2, status="error", error="GitHub 搜索失败 HTTP 403")
        cursor2 = db.get_cursor()
        check("失败任务不推进游标", cursor2["last_window_to"] == "2026-08-13", str(cursor2))
        rows = db.recent_tasks(5)
        check("失败任务带错误信息", rows[0]["status"] == "error"
              and "HTTP 403" in rows[0]["error"])

        # 文件指纹去重：新文件 → 扫；窗口内已扫 → 跳过；重新索引（窗口起点晚于上次扫描）→ 再扫
        check("新文件应扫描", db.should_scan_file("a/b/c.py", "2026-08-13") is True)
        db.mark_file_scanned("a/b/c.py")
        check("窗口内已扫文件跳过", db.should_scan_file("a/b/c.py", "2026-08-13") is False)
        check("窗口起点晚于上次扫描 → 重扫", db.should_scan_file("a/b/c.py", "2099-01-01") is True)
        check("无窗口参数时不重复扫", db.should_scan_file("a/b/c.py", "") is False)


def test_build_queries():
    print("== build_queries（语言分查 + 全局兜底）==")
    qs = GitHubCodeSearch.build_queries(["py", "js", "ts", "yaml", "yml"], pattern="sk-")
    texts = [q for q, _ in qs]
    check("py→python 查询", "sk- language:python" in texts, str(texts))
    check("js→javascript 查询", "sk- language:javascript" in texts, str(texts))
    check("ts→typescript 查询", "sk- language:typescript" in texts, str(texts))
    check("yaml/yml 合并为 language:yaml", "sk- language:yaml" in texts, str(texts))
    check("语言查询无重复", len(texts) == len(set(texts)), str(texts))
    # 全部可映射扩展名 → 无全局兜底查询
    qs2 = GitHubCodeSearch.build_queries(["py"], pattern="sk-", global_pages=1)
    check("全部可映射时无全局查询", all("language:" in q for q, _ in qs2), str(qs2))
    # 含无语言扩展名（env/conf/cfg）→ 追加全局兜底查询（任意文件类型）
    qs3 = GitHubCodeSearch.build_queries(["py", "env", "conf", "cfg"], pattern="sk-", global_pages=1)
    check("无语言扩展名触发全局兜底", any("language:" not in q for q, _ in qs3), str(qs3))
    check("全局兜底页数为 1", any("language:" not in q and p == 1 for q, p in qs3), str(qs3))
    # 兜底页数可为 0（关闭全局查询）
    qs4 = GitHubCodeSearch.build_queries(["env"], pattern="sk-", global_pages=0)
    check("关闭全局查询后 env 仍有兜底", qs4 and qs4[0][0] == "sk-", str(qs4))
    # 自定义查询词
    qs5 = GitHubCodeSearch.build_queries(["py"], pattern="sk-proj-")
    check("自定义查询词生效", qs5[0][0] == "sk-proj- language:python", str(qs5))


def test_tester():
    print("== key_tester（mock HTTP 响应）==")
    from unittest import mock

    # 1. 端点选择：记录 api_base_url 优先，其次端点库默认地址
    rec = {"api_base_url": "https://api.openai.com/v1", "provider": "OpenAI"}
    check("记录中的 api_base_url 优先", pick_base(rec) == "https://api.openai.com/v1")
    rec2 = {"api_base_url": "", "provider": "OpenRouter"}
    check("无记录时用端点库默认地址", pick_base(rec2) == "https://openrouter.ai/api/v1")
    rec3 = {"api_base_url": "", "provider": "不存在的服务商"}
    check("两者皆无返回 None", pick_base(rec3) is None)

    class FakeResp:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    # 2. 200 → active，统计模型数
    with mock.patch("gks.key_tester.requests.get",
                    return_value=FakeResp(200, {"data": [{"id": "gpt-4o"}, {"id": "gpt-4"}]})) as m:
        r = test_key("sk-test", "https://api.openai.com/v1")
        check("200 → active", r["verdict"] == "active" and r["ok"], str(r))
        check("统计模型数", r["models"] == 2, str(r))
        url = m.call_args[0][0]
        check("请求 URL 为 {base}/models", url == "https://api.openai.com/v1/models", url)
        check("携带 Bearer 头", m.call_args[1]["headers"]["Authorization"] == "Bearer sk-test")

    # 3. 401 → invalid
    with mock.patch("gks.key_tester.requests.get",
                    return_value=FakeResp(401, {"error": {"message": "Invalid API key"}})):
        r = test_key("sk-bad", "https://api.openai.com/v1")
        check("401 → invalid", r["verdict"] == "invalid" and not r["ok"], str(r))
        check("带错误消息", "Invalid API key" in r["error"], r["error"])

    # 4. 404 → unsupported（不改状态）
    with mock.patch("gks.key_tester.requests.get", return_value=FakeResp(404, None)):
        r = test_key("sk-x", "https://example.com/base")
        check("404 → unsupported", r["verdict"] == "unsupported", str(r))

    # 5. 429 → rate_limited
    with mock.patch("gks.key_tester.requests.get", return_value=FakeResp(429, None)):
        r = test_key("sk-x", "https://example.com/base")
        check("429 → rate_limited", r["verdict"] == "rate_limited", str(r))

    # 6. 网络异常 → network
    with mock.patch("gks.key_tester.requests.get",
                    side_effect=__import__("requests").ConnectionError("boom")):
        r = test_key("sk-x", "https://example.com/base")
        check("网络异常 → network", r["verdict"] == "network", str(r))


if __name__ == "__main__":
    test_extract()
    test_endpoints()
    test_normalize_base()
    test_preflight()
    test_truncate()
    test_rules()
    test_ai_parse()
    test_classify()
    test_build_queries()
    test_tester()
    test_db()
    test_tasks()
    print()
    if FAIL:
        print(f"共 {len(FAIL)} 项失败: {FAIL}")
        sys.exit(1)
    print("全部测试通过 ✓")
