# scanner.spacedo.org 部署与维护

入口：https://scanner.spacedo.org ，不带端口。服务器 `66.154.107.66`，部署目录 `/opt/github-key-scanner`。
部署复用宿主机已有 Nginx 的 HTTPS 入口；容器 Nginx 只发布 `127.0.0.1:18080`，Web 无宿主机端口。
仪表盘、API 和 CSV 下载均需要 Basic Auth。登录信息单独保存于交付目录的 `login-credentials.txt`。

## 运行配置

- 全球公开仓库监测；处理前必须确认 GitHub 返回 `repository.private=false`，未知和私有结果被丢弃。
- 搜索词 `sk-`，按语言分页并补充无语言的全局查询；每语言每轮最多 5 页，每页 100 条，全局补充 1 页。
- 每 7200 秒调度一轮，文件哈希与密钥 SHA256 指纹去重。运行中的扫描结束后再开始下一轮。
- 默认仅分析最近 30 天内推送过的公开仓库。支持最近 24 小时、7 天、30 天和自定义日期，时间设置在 Web 的扫描任务面板保存。
- AI 接口 `https://4router.net/v1`，模型 `gpt-5.6-luna`；每轮最多 200 个文件调用 AI，其余用规则分析。
- AI 只接收候选占位符、格式类别、静态服务商关键词和已知端点；原始代码、文件名和原始 URL 不发送给 AI。
- 数据库保留掩码、SHA256 指纹、脱敏的来源与分类；API 端点仅使用已知服务商的标准地址。完整候选、原始上下文、自由文本备注均不入库。
- 自动和手动有效性请求均已禁用。旧版明文数据库不迁移、不复用。

“全球”表示不限定仓库或组织。受 GitHub 索引、可搜索文件和单轮页数限制，不能保证穷尽所有公开仓库或每个泄露项。
REST API 使用旧版代码搜索语法，`is:public` 不是该接口支持的限定符；因此在处理搜索结果前执行可见性检查。
[GitHub 旧版代码搜索限制](https://docs.github.com/en/search-github/searching-on-github/searching-code)。

## 时间范围使用方法

打开扫描任务面板，选择范围，点击“保存范围”，再按需点击“立即扫描”。运行中的任务沿用启动时的设置，新设置从下一轮生效。

筛选以 GitHub 仓库的 `pushed_at`（最后一次推送）为准；先搜索候选文件，再核验每个候选仓库的公开状态和推送时间，只抓取、分析范围内的文件，并按仓库推送时间从新到旧处理。该时间不代表候选文件本身的修改时间，搜索页数上限也意味着这不是全部近期仓库的完整遍历。

预设范围是滚动时间；自定义日期按北京时间计算，包含开始日和结束日。任务表记录本轮的准确时间边界、核验仓库数、范围外文件数和无法核验数。无法核验的仓库被跳过；代码搜索失败、搜索结果不完整或存在无法核验的仓库时，本轮显示错误详情且不推进增量游标。

Web 与 daemon 共享 `/app/data/scan-settings.json`（`SCAN_SETTINGS_FILE`）。设置通过 `GET/PUT /api/scan/settings` 读取和保存，受现有登录保护；非法输入返回 400，保留此前设置。`--since` 仍用于本地文件去重窗口，仓库推送时间范围由此设置文件控制；`--full` 仅跳过文件去重。

## 文件与数据

```text
/opt/github-key-scanner/
  compose.yml
  Dockerfile.production
  .env                         # root:root 600，运行凭据
  nginx.conf
  reload-ingress.py
  secrets/scanner.htpasswd      # root:101 640，散列密码
  source/                      # 上游代码及本次脱敏修改
```

源码基线：`0b1eb0716a13d1a3f9c45001a6af3cd631f94d60`。
Web 和 daemon 共用命名卷 `spacedo-scanner_scanner-data`，数据库 `/app/data/monitor.db`。
此前启动验证生成的空 `keys.db` 未复用。

## 日常操作

在服务器 `/opt/github-key-scanner` 执行：

```sh
docker compose --profile scanner ps
docker compose --profile scanner up -d --build
docker compose --profile scanner logs --tail 50 daemon
docker compose exec -T web python main.py status
docker compose exec -T web python tests/test_monitoring_privacy.py
docker compose exec -T web python tests/test_scan_scope.py
docker compose exec -T web python tests/test_core.py
```

修改 `.env` 后，用 `docker compose --profile scanner up -d --force-recreate daemon` 应用扫描配置。
`docker compose down` 保留数据卷；不要添加 `-v`，以免删除监测记录。
备份使用 Python/SQLite 的 `Connection.backup()` 生成一致性快照，不应直接复制正在写入的 SQLite 主文件。

## HTTPS 与续期

宿主机入口文件为 `/etc/nginx/conf.d/scanner-http.conf` 和 `scanner-https.conf`。
HTTP 跳转 HTTPS，带显式端口的 Host 返回 421。
证书目录 `/etc/letsencrypt/live/scanner.spacedo.org`，初始证书到期日 2027-01-11。
`certbot.timer` 负责续期，部署钩子 `/etc/letsencrypt/renewal-hooks/deploy/60-scanner-ingress` 重载现有 HTTPS Nginx。
服务器有两个 Nginx 实例、共享 PID 文件；维护入口使用 `python3 /opt/github-key-scanner/reload-ingress.py`，它通过 443 监听进程定位正确实例。

## 验收记录

公开 HTTPS 证书验证、401/200 登录保护、API 和脱敏 CSV、有效性接口 403、HTTP 跳转、显式端口拒绝、18080 无公网访问、容器重建后的持久化、现有其他网站连通性，以及证书续期模拟均已通过。
源码的 13 项脱敏回归测试、上游核心测试、JavaScript 语法和 Git diff 格式检查已通过。
时间范围功能的 9 项测试、桌面/手机浏览器验证和公网登录后的范围设置验证均通过。20 个候选仓库的真实抽样核验全部超出 30 天，全部被排除；升级后原有 73 条记录保留。
实际扫描与后台服务的最终记录见交付目录的 `deployment-report.md` 和 `verification.json`。
