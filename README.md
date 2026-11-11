# 湖南省公立医院 HIS 中标情报分析系统

按湖南 14 个市州分区，展示每家公立医院**最近 2 次** HIS 相关中标记录：项目名称、采购单位、中标单位、HIS 厂家、金额、日期、**备注/标签/处理状态**，并可跳转公告原文、下载招标参数附件、组合筛选、导出 Excel。

> 当前状态（2026-11-10 实测）：已跑通真实数据闭环。库内 **162 条公告 / 117 家医院 / 14 个市州**（最近 2 次视图 145 条），覆盖全部市州；有效中标记录 214 条，识别品牌 15 个，未识别占比 **54.7%**，另有 128 条进入「待核」队列；附件 **214 条**（公告原文 162 + 正文链接 52；其中直链留存 14、失效 20、未下载 18——交易中心包件页或超出单公告下载上限），支持手工上传。**全部为真实公开公告，无假数据。**

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
python main.py collect        # 采集（默认 hunan_ccgp + hunan_gov_search + huaihua_ggzy）
python main.py import-records 中标数据.csv   # 导入/更新已有数据（按公告链接合并，备注保留）
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
python main.py collect --resume              # 从上次中断的页/站点群继续
python main.py crawl-state                   # 查看源冷却/失败归因/断点游标；--reset hunan_ccgp 清零冷却
python main.py fill-attachments --source hunan_ccgp --limit 50   # 从正文/交易中心页回补附件
python main.py import-hospitals 医院名录.csv   # 列名：name,city,level,source；加 --master 标记为主数据
python main.py hospital-unmatched               # 未匹配医院 + 归并建议
python main.py brand-recompute                  # 按包件/集成商规则重算品牌
python main.py reparse --limit 50               # 按详情页表格回补金额/包件/第一中标候选人
python main.py check-attachments --limit 50     # 复检附件失效
python main.py import-records 中标数据.xlsx     # 列名：公告链接,项目名称,医院,市州,中标日期,中标单位,HIS厂家,中标金额(元),备注,标签,处理状态
python main.py stats                            # 控制台输出质量/市州/品牌统计
```

## 2. 页面与功能

| 区域 | 说明 |
| --- | --- |
| 指标卡 | 公告数、覆盖医院、覆盖市州、未识别厂家占比、附件数 |
| 14 市州卡片 | 点击筛选该市州；全部市州均展示（含 0 条） |
| 最近 2 次表格 | 市州 / 医院 / 项目 / 中标单位 / HIS 厂家 / 金额 / 日期 / 备注 / 原文 / 附件 |
| 备注管理 | 每条中标记录可添加/修改文字备注、标签（逗号分隔）与处理状态（跟进中/已联系/重点客户/已成交/待评估/无需跟进，可自定义）；按公告 URL 稳定关联，重新采集或导入不会丢失 |
| 组合筛选 | 市州 + 医院 + HIS 厂家 + 公告日期区间 + 金额区间（万元） + 关键词 + 是否有备注/标签 + 标签 + 处理状态；一键清空；记录条数与导出结果均跟随筛选 |
| 数据更新 | 页面内增量采集（可暂停/继续/停止）与 CSV/XLSX 导入；导入按公告链接合并，已存在只更新字段、备注保留 |
| 医院时间线 | 点医院名查看历次采购记录（含备注摘要） |
| 品牌份额 | 按公告数统计，含「未识别」 |
| 数据质量 | 详情不可达、缺金额、未归一厂家占比、待核数、缺参数/招标附件、附件按类型/来源统计 |
| 附件管理 | 每条公告可上传参数文件（PDF/Word/Excel/压缩包等），自动记录类型、来源、SHA256、上传人、时间并写审计；支持下载、预览、失效复检；公告原文、可下载直链、手工上传三来源并存 |
| 新中标提醒 | 增量采集新增命中后写入站内提醒，顶栏红点显示未读数，列表可跳原文、一键全部已读；不接外部通知渠道 |
| 数据治理 | 「医院未匹配」（名录导入 → 别名归并建议 → 采纳/手工合并）、「待核中标」（集成商/低置信记录人工确认厂家）、「品牌规则」（在线增删改，规则优先命中供应商名称） |
| 数据源健康 | 各站点最近检测结果 |
| 采集任务 | 页面触发采集、暂停/继续/停止、查看任务日志 |
| 导出 | 一键导出 4 个 sheet 的 Excel；主表含备注/标签/处理状态列，范围跟随当前筛选 |
| 天眼查 | 未配置 `TYC_API_KEY` 时禁用并提示，不抓网页 |

角色：`admin`（全部权限）、`analyst`（采集与维护/备注/导入）、`viewer`（只读）。登录与关键操作写入 `audit_logs`，备注修改额外写 `change_history`。

### 备注与数据更新的稳定性

- 备注表 `record_notes` 以 `ann_url`（公告 URL，公告表唯一键）关联，不依赖自增 id；重新采集、重复导入、更新公告字段都不会删除或重建公告行，因此备注始终保留。
- 导入按公告链接合并：已存在公告只更新本次提供的字段；备注/标签/状态仅在文件对应列非空时覆盖，空值不会清空已有备注。
- 导入必填列：`公告链接`、`项目名称`；其余列可选。金额列按「元」解释，文本含「万元/亿元」会自动换算。
- 筛选口径与「最近 2 次」一致：先在每家医院最近 2 次内取数，再叠加条件；导出使用同一套筛选参数。

## 3. 数据源实测状态

| 数据源 | 结论 | 说明 |
| --- | --- | --- |
| 湖南省政府网站站群搜索 `searching.hunan.gov.cn` | **可用** | 按 14 市州站群 scope + `searchfields=title` 检索；时间过滤参数失效，改为本地 `COLLECT_SINCE` 过滤 |
| 怀化市公共资源交易中心 `ggzy.huaihua.gov.cn` | **可用** | 列表 `/c116113/list_N.shtml` 分页 + 详情正文，字段含中标单位/金额 |
| 中国政府采购网 `search.ccgp.gov.cn` | **参数可用，当前 IP 被风控** | 返回「访问过于频繁」；附件直链 403。需境内网络/合规出口重试，不绕过 |
| 湖南省政府采购网 `ccgp-hunan.gov.cn` | **可用** | 2026-11-10 实测可检索/解析，已作为默认源；按查询计划检索「医院信息 / 信息系统 / 电子病历 / HIS / PACS / LIS / 互联网医院 / 智慧医院 / 医保移动支付 / 银医」，再按「中标公告 + 标题含医院/HIS 关键词」过滤。详情接口的 `attachmentList` 多数为空，但正文里仍有 OSS 直链与交易中心包件页，已由 `fill-attachments` 回补；OSS 返回 510/失效链接只标 `download_failed`，不绕过 |
| 各市州交易中心（岳阳/益阳/郴州等） | **部分可用** | 站群搜索可发现其公告链接；直连详情受站点可达性影响 |
| 永州市公共资源交易中心 `ggzy.yzcity.gov.cn` | **详情可用，命中少** | 中标公示正文含真实「招标文件」附件直链（`downloadFile?fileid=`）；近 24 页未见医院 HIS 项目，暂不列为常规源 |
| 中国招标投标公共服务平台 `cebpubservice.com` | **不可自动化** | 接口返回 `success:false`，页面为 JS/签名，未接入 |
| 医院官网公示（省人民医院、郴州一医院等） | **多数不可达/JS 渲染** | 首页可访问但采购栏目需 JS 加载，未强行抓取 |
| 湖南省卫健委 `wjw.hunan.gov.cn` | **站点可用，未找到全量名录** | 未见可直接下载的公立医院/等级医院完整名单，改用 CSV 模板导入 |
| 天眼查 | **仅官方 API 占位** | 无 key 即禁用；不做网页抓取、不绕验证码 |

字段缺口会如实标记：详情不可达写 `fetch_status='detail_unreachable'`，无金额则该字段为空、页面显示「未获取」，不补造。

## 4. 取数口径

- **检索编排**：`app/search_plan.py` 按站点能力把关键词展开成查询计划（省政采：核心词「医院信息 / 信息系统 / 电子病历 / HIS」+ 院内业务系统词「PACS / LIS / 互联网医院 / 智慧医院 / 医保移动支付 / 银医」；站群搜索：前 3 个关键词 × 15 个站点群；怀化：栏目翻页）。候选先按 HIS 相关度排序，再做 URL 去重与跨源去重（标题归一 + 发布日期 + 采购单位，另查库内其他来源同题同日公告），最后逐条解析入库。思路借鉴 Agent-Reach 的处理顺序（有序后端、失败分类），未复制其代码。
- **结构化抽取**：入库时优先解析详情页表格——表头式（包号/中标供应商/中标金额）逐包件入库，标签式（`中标供应商 | 某某公司`）取字段，候选人公示表只取排名第一的候选人；拿不到表格才退回文本正则。第一中标候选人的金额来自「投标报价」，`amount_raw` 会标注「（第一中标候选人报价）」，不把它伪装成已定标金额。
- **失败归因与冷却**：`app/source_state.py` 把请求失败分为 `blocked / robots / rate_limited / upstream_broken / network / timeout / parse / http`，按类型和连续失败次数冷却（风控 1 小时起、限流 5 分钟起、超时 1 分钟起），状态写入 `source_runtime`；`collect` 每个源执行前先查冷却，冷却期内直接跳过并在任务统计里记 `cooldown_skipped`，确认站点恢复后用 `crawl-state --reset <源>` 手动清零。被封站点只标记、不换 IP/不绕验证码。
- **断点续采**：每个查询/站群的翻页进度写入 `crawl_cursor`，`collect --resume` 从游标处继续；正常跑完即删除对应游标，中断（停止/异常）则保留。默认 `collect` 仍从第一页开始，不改变原有全量行为。
- **附件补齐**：`fill-attachments` 回读公告详情，除 `attachmentList` 外还解析正文 `<a>`——OSS 直链、`downloadFile?fileid=` 下载接口直接下载留存；交易中心包件页等非直链只登记为 `source=body_link, status=unverified` 并写备注，不强行抓取；失效/510/404 统一标 `download_failed`，不补造文件。附件发现顺序参考 Agent-Reach 的 public-first 思路（先读公开公告正文，再考虑人工上传），未复制代码。
- **HIS 相关**：标题命中关键词得 3 分、正文命中得 1 分，总分 ≥ 3 才入库；纯英文关键词（HIS / PACS / LIS）按词边界匹配，避免误命中 THIS、ANALYSIS。关键词在 `config/his_keywords.txt`，可增删。
- **中标公告**：标题/类型须为中标的公告；「流标/废标/终止/更正」排除。
- **最近 2 次**：按医院分组，先按（医院, 中标日期）合并同一天多包，再按日期倒序取前 2 个日期；同一天的全部包件公告记录合并展示（页面显示包件数），金额取当日最大包件金额，中标单位/品牌去重合并；`is_void=1` 排除；无日期的公告按 id 各自保留，不并入同一天。
- **去重**：`announcements.url` 唯一；`content_hash`（去空白后的全文 SHA256）用于识别同一 URL 内容变更，变更写 `change_history`。
- **市州归属**：优先按公告 URL/来源站点域名判定，其次采购单位名称、标题、正文前 200 字、来源提示；无法判定则「未归属」，不强行分配。
- **品牌识别**：先查规则库（供应商名称命中 0.9）；非集成商只认供应商名称，不再用「正文提到某品牌」强行归类；集成商（电信/联通/移动/铁塔/数智/系统集成等）需正文或所在包件佐证才给 0.55–0.6，否则留在「待核」；代理机构（招标/咨询/采购中心）标记 `is_winner=0`，不计入中标单位与未识别占比。规则可在「数据治理 → 品牌规则」在线维护，也支持 `config/supplier_brand_rules.csv`。

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
│  ├─ search_plan.py      多源检索编排：查询计划、跨源去重、相关度排序
│  ├─ source_state.py     失败归因、分站点冷却退避、断点续采游标
│  ├─ page_extract.py     详情页结构化抽取：表格包件、标签字段、第一候选人、金额单元格
│  ├─ aggregate.py        最近 2 次、市州汇总、品牌份额、质量统计、待核/通知/未匹配
│  ├─ export.py           Excel 导出（4 sheet，含备注/附件链接列，支持筛选）
│  ├─ importer.py         CSV/XLSX 导入（按公告链接合并、备注保留）
│  ├─ attachments.py      附件上传/下载/预览/失效复检/按类型统计
│  ├─ hospital_master.py  医院名录模板、导入、别名归并建议与手工合并
│  ├─ brands_ops.py       包件拆分、集成商识别、品牌重算、待核确认、规则维护
│  ├─ tianyancha.py       天眼查开放平台 API 占位
│  ├─ agent_reach.py      Agent-Reach 只读回退通道适配（可选，未装自动降级）
│  ├─ version.py          版本号与 GitHub Releases 更新检查
│  ├─ web.py              Flask 路由 / API / 登录 / 审计
│  ├─ collectors/         站点适配器（省政采、站群搜索、怀化、国政采、详情解析）
│  └─ templates/index.html 单页仪表盘（原生 JS，无外部 CDN）
├─ assets/                应用图标 app.ico（exe/任务栏/favicon 统一）
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
| `REQUEST_INTERVAL_SEC` | 2.0 | 请求间隔（限速），当前 `.env` 为 2.0；可按目标站点承受度调大 |
| `REQUEST_TIMEOUT_SEC` | 20 | 单请求超时 |
| `DOWNLOAD_ATTACHMENTS` | 1 | 是否下载附件 |
| `ATTACHMENT_CHECK_HOURS` | 24 | 附件失效定时复检间隔（小时，0=关闭） |
| `MAX_UPLOAD_MB` | 50 | 手工上传参数文件的单文件大小上限（MB） |
| `ADMIN_USER` / `ADMIN_PASSWORD` | admin / admin123 | 初始管理员 |
| `TYC_API_KEY` | 空 | 天眼查开放平台 key，空=模块禁用 |
| `AGENT_REACH_ENABLED` | 1 | Agent-Reach 回退通道开关（0=禁用） |
| `AGENT_REACH_TIMEOUT_SEC` | 60 | 单次 agent-reach CLI 调用超时 |

`.env` 已在 `.gitignore` 中，不会入库。

## 7. Windows 可执行程序与版本升级

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

- 产物：`dist\his-intel\his-intel.exe`（服务入口 `his-intel.exe serve`）+ 版本化升级包 `dist\his-intel-vX.Y.Z-win64.zip`（exe 已嵌入 `assets\app.ico` 多尺寸图标）。
- 版本号唯一定义在 `app/version.py`；`his-intel.exe version` 显示当前版本并联网检查 GitHub Releases 最新版，`--no-check` 只看本地；页面标题旁版本徽标点击同样触发检查。
- **升级方式**：下载最新 zip，解压覆盖旧的 `his-intel` 目录即可。`data\`（数据库/附件/导出）不在 zip 内不会被覆盖；`config\` 与 `.env` 覆盖安装时保留本地版本（zip 内不含二者时自动跳过）。
- 发布流程：改 `app/version.py` → 跑 `build_exe.ps1` → `gh release create vX.Y.Z dist\his-intel-vX.Y.Z-win64.zip`。

## 7.1 Agent-Reach 回退通道（数据采集增强）

本项目已实际集成 [Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach)（MIT）作为**可选只读回退通道**（`app/agent_reach.py`）：

- 详情页直连失败（`detail_unreachable`，如 JS 渲染、IP 风控）时，自动调用 `agent-reach read <url>`（底层 Jina Reader → Exa 有序回退）读取正文兜底；成功后正常走解析入库，`extract_meta.agent_reach_fallback=true` 可追溯，正文里的 pdf/docx 等直链同时登记为附件。
- 数据源体检新增「Agent-Reach 通道」行：未安装/禁用/可用状态如实显示；`his-intel.exe agent-reach` 查看状态。
- **未安装 CLI 时全部静默降级，采集行为与原来完全一致**；安装方式见官方仓库 `docs/install.md`（注意：PyPI 上的 `agent-reach` 是冒名包，禁止 pip 安装）。
- 合规边界不变：仅只读公开页面，不绕验证码、不采个人信息。

## 8. 合规声明

- 仅采集公开公告，不采集个人信息；遵守目标站点 robots 与使用条款，按 `.env` 限速（当前 2.0 秒/请求），可暂停/停止。
- 不抓取天眼查网页、不绕验证码、不伪造/共享账号；天眼查仅走官方开放平台 API。
- 抓不到就标记「未获取 / 详情不可达」，不编造公告、金额、附件。
- 数据仅用于内部分析，对外引用请以原文链接为准。

## 9. 已知限制

1. `search.ccgp.gov.cn` 对当前出口 IP 返回风控；需境内网络环境重跑，系统已如实标记 `detail_unreachable`，不影响其余源。
2. 政府站群时间过滤参数无效，按 `COLLECT_SINCE` 本地过滤，翻页越深命中越少。
3. **参数/招标文件获取受站点限制**：省政采中标公告的 `attachmentList` 多数为空，但正文仍含 OSS 直链和交易中心包件页；`fill-attachments` 回补 144 条公告后发现正文链接 52 条，其中直链留存 14 条、20 条因 OSS 返回 510/404 失效（只标 `download_failed`，不绕过）、18 条为交易中心包件页/超出单公告 3 个下载上限（只登记不硬抓）。当前「招标文件」类仅 6 条（2 条有效），参数文件仍主要靠页面「附件管理」手工上传（已记录类型/SHA256/上传人/审计，支持失效复检）。附件解析器支持 pdf/doc/docx/xls/xlsx/zip/rar/ofd。直连失败的详情页另走 Agent-Reach 只读回退通道（见 7.1）。
4. 医院主数据为采集发现式；省卫健委未见可直接下载的完整名录，需用 `/api/hospitals/master/template` 模板导入，导入后自动给出归并建议并可手工合并。
5. 品牌识别为规则 + 人工确认：未识别 117/214（54.7%），其中大量为运营商/集成商中标、正文无厂家佐证，已进入「待核」队列（128 条），不强行归因。
6. 「最近 2 次」的同日多包已合并计数，但历史数据中部分公告正文未逐包列供应商，包件级拆分以正文明确出现「包N」时为限。

## 10. 后续增强（本轮未做）

- 邮件/飞书等外部通知渠道（当前仅站内提醒）
- 厂家竞品对比、医院画像、区域渗透分析
- 金额异常与重复公告交叉校验
- 参数文件全文检索与品牌型号抽取
- 永州等市州交易中心的定时增量接入（当前仅确认附件直链模式）

## 11. 待确认的外部条件

1. 运行网络是否可切到境内出口（决定国政采/省政采能否启用）。
2. 是否提供天眼查开放平台 API key（不提供则该模块长期禁用）。
3. 是否有卫健委/等级医院名录文件（用于覆盖「全部公立医院」口径）。
4. 目标年份范围（当前默认 2018 起，可改 `COLLECT_SINCE`）。
