# 湖南省公立医院 HIS 中标情报分析系统

按湖南 14 个市州分区，展示每家公立医院**最近 2 次** HIS 相关中标记录：项目名称、采购单位、中标单位、HIS 厂家、金额、日期，并可跳转公告原文、下载招标参数附件、导出 Excel。

> 当前状态（2026-11-07 实测）：已跑通真实数据闭环。样例库 **15 条公告 / 15 家医院 / 4 个市州**（岳阳 7、怀化 5、益阳 2、郴州 1），命中品牌 众阳健康、中联、创业慧康，其余如实记为「未识别」；附件 0 条（原因见「已知限制」）。**全部为真实公开公告，无假数据。**

---

## 1. 快速开始（Windows）

前置：Python 3.10+（开发环境为 3.14），可访问外网。

```powershell
cd d:\D_file\jgq_ys\newjob\tianyancha

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env        # 按需修改账号、端口、采集参数
python main.py init-db        # 建表 + 种入 14 市州 / 品牌 / 供应商规则 / 管理员
python main.py health         # 数据源连通性检测
python main.py collect        # 采集（默认 hunan_gov_search,huaihua_ggzy）
python main.py export         # 导出 Excel 到 data/exports/
python main.py serve          # 启动 Web，默认 http://127.0.0.1:8765
```

也可双击 `scripts\start.bat` 直接启动服务（自动使用 `.venv`）。

默认账号：`admin / admin123`（首次部署请改 `.env` 中的 `ADMIN_PASSWORD` 后删除 `data/his_intel.db` 重建，或直接改库中 `users` 表）。

常用命令参数：

```powershell
python main.py collect --pages 3 --since 2023-01-01
python main.py collect --sources huaihua_ggzy
python main.py collect --keywords "医院信息系统,HIS系统"
python main.py import-hospitals 医院名录.csv   # 列名：name,city,level,source
python main.py stats                            # 控制台输出质量/市州/品牌统计
```

## 2. 页面与功能

| 区域 | 说明 |
| --- | --- |
| 指标卡 | 公告数、覆盖医院、覆盖市州、未识别厂家占比、附件数 |
| 14 市州卡片 | 点击筛选该市州；全部市州均展示（含 0 条） |
| 最近 2 次表格 | 市州 / 医院 / 项目 / 中标单位 / HIS 厂家 / 金额 / 日期 / 原文 / 附件 |
| 医院时间线 | 点医院名查看历次采购记录 |
| 品牌份额 | 按公告数统计，含「未识别」 |
| 数据质量 | 详情不可达、缺金额、未归一厂家占比、附件状态 |
| 数据源健康 | 各站点最近检测结果 |
| 采集任务 | 页面触发采集、暂停/继续/停止、查看任务日志 |
| 导出 | 一键导出 4 个 sheet 的 Excel |
| 天眼查 | 未配置 `TYC_API_KEY` 时禁用并提示，不抓网页 |

角色：`admin`（全部权限）、`analyst`（采集与维护）、`viewer`（只读）。登录与关键操作写入 `audit_logs`。

## 3. 数据源实测状态

| 数据源 | 结论 | 说明 |
| --- | --- | --- |
| 湖南省政府网站站群搜索 `searching.hunan.gov.cn` | **可用** | 按 14 市州站群 scope + `searchfields=title` 检索；时间过滤参数失效，改为本地 `COLLECT_SINCE` 过滤 |
| 怀化市公共资源交易中心 `ggzy.huaihua.gov.cn` | **可用** | 列表 `/c116113/list_N.shtml` 分页 + 详情正文，字段含中标单位/金额 |
| 中国政府采购网 `search.ccgp.gov.cn` | **参数可用，当前 IP 被风控** | 返回「访问过于频繁」；附件直链 403。需境内网络/合规出口重试，不绕过 |
| 湖南省政府采购网 | **本机不可达** | 适配器已保留，`enabled: false`，待境内网络验证 |
| 各市州交易中心（岳阳/益阳/郴州等） | **部分可用** | 站群搜索可发现其公告链接；直连详情受站点可达性影响 |
| 天眼查 | **仅官方 API 占位** | 无 key 即禁用；不做网页抓取、不绕验证码 |

字段缺口会如实标记：详情不可达写 `fetch_status='detail_unreachable'`，无金额则该字段为空、页面显示「未获取」，不补造。

## 4. 取数口径

- **HIS 相关**：标题命中关键词得 3 分、正文命中得 1 分，总分 ≥ 3 才入库。关键词在 `config/his_keywords.txt`，可增删。
- **中标公告**：标题/类型须为中标的公告；「流标/废标/终止/更正」排除。
- **最近 2 次**：按医院分组，`publish_date` 倒序、同日按入库 id 倒序，取 `ROW_NUMBER() <= 2`；`is_void=1` 排除。同一天多个包件按公告计 1 次，金额取该公告最大包件金额，中标单位去重合并。
- **去重**：`announcements.url` 唯一；`content_hash`（去空白后的全文 SHA256）用于识别同一 URL 内容变更，变更写 `change_history`。
- **市州归属**：优先按公告 URL/来源站点域名判定，其次采购单位名称、标题、正文前 200 字、来源提示；无法判定则「未归属」，不强行分配。
- **品牌识别**：供应商名称规则命中置信度 0.9；正文命中 0.45，页面标「待核」。无法匹配归「未识别」，不强行归类。规则见 `config/supplier_brand_rules.csv`，品牌见 `config/brands.json`。

## 5. 目录结构

```
tianyancha/
├─ app/
│  ├─ cli.py              命令行入口
│  ├─ config.py           .env / 路径 / 关键词 / 品牌加载
│  ├─ db.py               建表、初始化、连接、日志与审计
│  ├─ normalize.py        医院名/金额/供应商/品牌/相关度归一
│  ├─ regions.py          14 市州、区县映射、站群 scope、市州判定
│  ├─ pipeline.py         采集→解析→去重→入库→附件留存
│  ├─ aggregate.py        最近 2 次、市州汇总、品牌份额、质量统计
│  ├─ export.py           Excel 导出（4 sheet）
│  ├─ tianyancha.py       天眼查开放平台 API 占位
│  ├─ web.py              Flask 路由 / API / 登录 / 审计
│  ├─ collectors/         站点适配器（站群搜索、怀化、国政采、详情解析）
│  └─ templates/index.html 单页仪表盘（原生 JS，无外部 CDN）
├─ config/                关键词、品牌、供应商→品牌规则
├─ data/                  SQLite、附件、导出（运行时生成，不入库）
├─ docs/数据字典.md        表结构、字段、枚举、口径
├─ scripts/               start.bat / build_exe.ps1
├─ main.py                入口
└─ requirements.txt
```

## 6. 配置（.env）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APP_HOST` / `APP_PORT` | 127.0.0.1 / 8765 | Web 监听地址 |
| `APP_SECRET` | dev-secret-change-me | Flask session 密钥，生产必改 |
| `DB_PATH` | data/his_intel.db | SQLite 路径 |
| `ATTACHMENT_DIR` | data/attachments | 附件留存目录 |
| `EXPORT_DIR` | data/exports | Excel 输出目录 |
| `COLLECT_SINCE` | 2018-01-01 | 增量/回补起始日期 |
| `COLLECT_MAX_PAGES` | 6 | 每关键词翻页上限 |
| `REQUEST_INTERVAL_SEC` | 4.5 | 请求间隔（限速），测试可临时调小 |
| `REQUEST_TIMEOUT_SEC` | 20 | 单请求超时 |
| `DOWNLOAD_ATTACHMENTS` | 1 | 是否下载附件 |
| `ADMIN_USER` / `ADMIN_PASSWORD` | admin / admin123 | 初始管理员 |
| `TYC_API_KEY` | 空 | 天眼查开放平台 key，空=模块禁用 |

`.env` 已在 `.gitignore` 中，不会入库。

## 7. Windows 可执行程序

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

产物在 `dist\his-intel\`：`his-intel.exe` 为服务入口（`his-intel.exe serve`），同目录需保留 `config\`、`.env`、`data\`。模板与配置已随包打入，仍建议把 `config/` 放在 exe 同级以便人工维护品牌与关键词。

## 8. 合规声明

- 仅采集公开公告，不采集个人信息；遵守目标站点 robots 与使用条款，默认 4.5 秒/请求、可暂停停止。
- 不抓取天眼查网页、不绕验证码、不伪造/共享账号；天眼查仅走官方开放平台 API。
- 抓不到就标记「未获取 / 详情不可达」，不编造公告、金额、附件。
- 数据仅用于内部分析，对外引用请以原文链接为准。

## 9. 已知限制

1. `search.ccgp.gov.cn` 对当前出口 IP 返回风控；需境内网络环境重跑，系统已如实标记 `detail_unreachable`，不影响其余源。
2. 政府站群时间过滤参数无效，按 `COLLECT_SINCE` 本地过滤，翻页越深命中越少。
3. 部分站点公告正文无附件直链（只写「见附件」），本轮样本附件为 0；附件解析器已支持 pdf/doc/docx/xls/xlsx/zip/rar/ofd。
4. 医院主数据为采集发现式，尚缺卫健委全量名录；`import-hospitals` 已就绪，待名录文件。
5. 品牌规则为静态规则 + 人工维护表，未做模型抽取；未识别占比在数据质量看板可见。

## 10. 后续增强（本轮未做）

- 新中标监控与订阅提醒（邮件/飞书）
- 厂家竞品对比、医院画像、区域渗透分析
- 金额异常与重复公告交叉校验
- 附件批量归档 + 全文检索（参数文件品牌型号抽取）
- 附件失效定时复检
- 卫健委名录导入与医院别名人工合并界面

## 11. 待确认的外部条件

1. 运行网络是否可切到境内出口（决定国政采/省政采能否启用）。
2. 是否提供天眼查开放平台 API key（不提供则该模块长期禁用）。
3. 是否有卫健委/等级医院名录文件（用于覆盖「全部公立医院」口径）。
4. 目标年份范围（当前默认 2018 起，可改 `COLLECT_SINCE`）。
