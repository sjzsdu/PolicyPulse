# PolicyPulse

每天抓一次中央与部委的会议/政策通稿，抽政策信号，映射到行业与个股，产出一份
**每条结论都能点回原文**的"潜在受益个股排序"。个人自用、单机、单进程。

> 结论是"政策受益度排序 + 理由"，**不是涨幅预测，不是买卖点**。所有产物自带免责声明。

## 安装方式（重要）

**只支持 editable 安装**：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

不要 `pip install .`（非 editable）。`sources.yaml`、`data/theme_alias.csv` 与
`data/theme_business.csv` 是随仓库走的数据文件，`config.py` 按仓库根定位它们，
打进 wheel 的非 editable 安装会找不到。
package-data 方案未采用——这个选择是明确的，不是两可。

## 一条命令跑通

```bash
policypulse selftest          # 先确认 tongstock / 网络 / LLM 三件事的状态
policypulse run               # 抓取 → 抽取 → 映射 → 打分 → 出日报
```

常用开关：`--date`、`--since N`（只在**显式补跑**时用）、`--sources <id...>`、
`--refresh-market`、`--no-dedupe`（当天重跑时关掉跨 run 去重）。

**日报只收当日增量**（本 issue 第 1 点）：默认窗口是 `[目标日-1, 目标日]`
（`daily_window_days: 2`，容忍晚间与次日凌晨发布的稿子），只收 `published_at` 落在
窗口内的稿件；**没有「窗口内无更新就回退抓最近几条」的兜底**，窗口内确实没有新稿时，
日报就写「今日无新增政策 / 事件」——空日报是常态，不是故障。历史回补必须显式
`policypulse run --since N`（窗口 `[目标日-N, 目标日]`），它的产物同样是按日期落盘的
独立日报，不会混进当天那份。概览里写明本次真实统计窗口。

跑完的产物（`reports/` 全部是本机交付物，不入库）：

- `reports/YYYY-MM-DD.md` — 日报（主交付，给读者看：本期概览 → 受益个股 → 主题关联 →
  待确认 → 其他政策信号 → 泛政策表述 → 个股深度研判 → 信息来源）
- `reports/YYYY-MM-DD.html` — 同一份内容的单页展示，浏览器直接打开
- `reports/YYYY-MM-DD.json` — 同一批结论的结构化版本（**复核用**：run_id、抓取告警、
  快照绝对路径、逐源合规台账 `stats.sources_detail`、分区键与逐条判定依据）
- `data/policypulse.db` — SQLite：documents / signals / signal_ticker / runs …
- `data/snapshots/<run_id>/*` — 原文快照（不入库，后缀跟随原件）
- `inbound/<source_id>/` — 人工下载的原文（目录骨架入库，用户放进去的文件不入库）

**md / html 只写给读者看**：运行期指标（run_id、抓取告警、窗口内外计数、闸门统计、
LLM 开没开、tongstock 调用次数）、内部 ID（事件/信号/快照路径）、robots 合规台账、
复核命令一律不进这两个文件——它们在 JSON 与数据库里，复核照旧拿得到。
`tests/test_report.py::test_product_carries_no_developer_facing_content` 钉住了这条边界。

其它命令：

```bash
policypulse sources                    # 列出信息源清单
policypulse show <signal_id>           # 打印一个信号的完整溯源链
policypulse verify --run <run_id>      # 重抓原文核对：一致 / 内容已变 / 链接失效
policypulse run --date 2026-10-09 --since 7
policypulse run --sources gov_zhengce xinhua_pbc pbc_inbound
```

## 依赖的外部工具

**tongstock**（本机 `/Users/juzhongsun/.local/bin/tongstock`，通达信 TDX 数据）是
A 股基础信息、行业/概念板块与成分股的数据来源，必须可用：

```bash
tongstock block files                      # 确认 TDX 服务器可达
tongstock stockinfo --exchange sh --json   # 个股市值
tongstock block list -f block_gn.dat       # 269 个概念板块
tongstock block show 低空经济 -f block_gn.dat
```

结果会缓存到 `data/cache/`，默认 24 小时内不重复打 TDX；`--refresh-market` 强制刷新。
`block show` 返回的股票名不可信（会显示成指数名），代码里一律用 `stockinfo` / `codes list` 的名称订正。

## 央行（pbc.gov.cn）怎么拿：robots 全站禁止下的换取法

`http://www.pbc.gov.cn/robots.txt` 是：

```
User-agent: Baiduspider
Allow: /
User-agent: *
Disallow: /
```

即**除了 Baiduspider，一律禁止抓取**。我们不会伪造 UA 去冒充 Baiduspider，也不会用
代理池规避——`sources.yaml` 里的 `pbc_xw` 因此保持 `enabled: false`，`http.py` 的
robots 四档判定会把任何针对该 host 的请求挡在前面。

五个候选方向都实测过（不是估的），结论如下：

| 方向 | 实测结果 | 结论 |
|---|---|---|
| 1. 转载源 · 新华网 `news.cn` | robots `User-Agent: * / Allow: /`；`fortune` 栏目日更，本轮命中 3 篇央行稿 | **采用**，`xinhua_pbc` |
| 1. 转载源 · 中国政府网 `gov.cn` | robots `Allow: /1`（放行）；但 `zhengce/zuixin` 1105 条里央行相关仅 5 条、最新 2023-10；`xinwen/yaowen` 20253 条停在 2023-03 | 不新增源：已有两条 gov.cn 源，转载覆盖陈旧 |
| 1. 转载源 · 人民网 `people.com.cn` | robots `Disallow:`（等于放行）但声明 `Crawl-delay: 120` | **不启用**：一轮 12 篇要 24 分钟。代码已按 `Crawl-delay` 限速，真要开就把 `enabled` 改 true 并接受耗时 |
| 1. 转载源 · 金融时报 `financialnews.com.cn`（央行主管） | robots 404 → `unavailable` 档（可抓，保守 5s + 告警）；但 `yh/` 银行频道停在 2024-04 | 不接入：内容停更 |
| 2. 央行官方公开接口/RSS | `pbc.gov.cn/rss.xml` 404；全站各栏目同 host，一律 `Disallow: /`；`sousuo.www.gov.cn`（政府网搜索接口）也是 `Disallow: /` | **不存在**合规的官方机器接口 |
| 3. 申请抓取授权 | 央行站点未公布任何数据授权/爬虫白名单申请入口，也没有公开的成本与周期说明 | **无公开申请路径**，需要走线下公函，个人自用场景不可持续 |
| 4. 人工/半自动 inbound | 用户本人浏览器下载属个人浏览行为，程序只解析本地文件 | **采用**，`pbc_inbound` |
| 5. AkShare 等第三方数据接口 | 实测可安装可用（`akshare` 1.19.1，MIT）；`macro_china_lpr()` / `macro_china_money_supply()` 能取到数据 | **不接入**：返回的是**数值序列**，没有原文 URL、没有可校验的 quote，过不了「quote 必须是原文子串」和 `verify` 回溯；且引入 pandas 等重依赖，与单机零运维目标不符 |

### 采用的两条路

**① 转载（`xinhua_pbc`，默认启用）** — 新华网财经栏目，robots 实测 `allowed`，
用 `title_pattern` 只收央行口径的稿子（中国人民银行/央行/货币政策/公开市场/降准/LPR/
再贷款/逆回购/MLF…），不把整个财经频道搬进来。

**② 人工入库（`pbc_inbound`，默认启用）** — 你自己下载央行原稿，放进目录即可：

```bash
# 1) 浏览器打开央行栏目，另存为（完整网页 或 只存文本都行）
# 2) 放进这个目录，文件名带日期，并给一个溯源 URL
inbound/pbc_inbound/2026-10-20-公开市场业务交易公告.html
inbound/pbc_inbound/2026-10-20-公开市场业务交易公告.url   # 一行：原文 URL

# 3) 照常跑
policypulse run --sources pbc_inbound
```

「另存为 + 手抄 URL」这一步可以用 `tools/capture_pbc_inbound.py` 省掉：它驱动
**ego-browser 的一次人工会话**（真实 Chromium + 真实 profile，你本人可见、可随时接管），
把页面 HTML 与 `page.url()` 直接写进 `inbound/pbc_inbound/`，文件名里的日期取页面发布时间：

```bash
# URL 由你从央行栏目页肉眼选定后粘过来；工具不做链接发现、不翻页
python3 tools/capture_pbc_inbound.py --url 'https://www.pbc.gov.cn/…/index.html'
python3 tools/capture_pbc_inbound.py --url '<URL1>' --url '<URL2>' --dry-run   # 先看脚本
```

它**不发 HTTP 请求**：请求由浏览器在你的会话里发出，等价于你手动另存；管线对
pbc.gov.cn 依旧零请求。三条硬约束写死在工具里——只取命令行给出的 URL、不伪造 UA/不挂代理、
遇验证码/登录墙 `handOff()` 交还控制权。**不要把它放进定时任务**：无人在场就不该跑，
那就成爬虫了。

2026-10-19 的一次真实会话（本节数字即该次产物）：从
`goutongjiaoliu/113456/113469/index.html`（新闻发布栏目）选了两篇公开稿入库——
《关于金融支持建设北京（京津冀）、上海（长三角）、粤港澳大湾区国际科技创新中心的指导意见》、
《2026年前三季度金融统计数据报告》；随后 `policypulse run` 的 **35 篇 / 87 条信号 /
480 行个股结论**里，央行相关文档 8 篇（`mof_czxw` 2 + `xinhua_pbc` 4 + `pbc_inbound` 2），
`policypulse verify` 为 **33 一致 + 2 一致（本地原件）**，链接失效 0。

溯源 URL 的三个来源（按优先级）：同名 `.url` 伴生文件 → 正文前 5 行的
`原文: https://…` / `来源链接: …` 标记 → HTML 的 `<link rel="canonical">` / `<base href>`。
**三者都没有的文件会被拒收并在告警区点名**——没有 URL 就没有 `source_url`，结论不可回溯，
宁可不出结论。

其它约定：

- 文件**不删不移**，跨 run 去重靠正文指纹，重复跑同一批不会重复抽取。
- `published_at` 优先取文件名里的 `YYYY-MM-DD`，其次正文里的日期；都取不到就留空
  显示「日期未知」，**不拿文件 mtime 冒充发布日期**。
- 入库文件同样受当日窗口约束：**能取到日期**且落在 `[目标日-1, 目标日]` 之外的，
  当天不进日报（要看就 `policypulse run --since N --sources pbc_inbound` 补跑）；
  取不到日期的按上一条显示「日期未知」并正常入库。
- 目录空着不是故障，只在告警区提示；成功率统计不把本地源算进分母。
- `policypulse verify` 对这类文档**不去重抓源站**（源站禁止抓取），改为与落盘的本地
  原件复算指纹，状态显示「一致（本地原件）」。

### 覆盖不到的部分（不拿劣质数据凑数）

- 央行**政策文件原文**（如《货币政策执行报告》正文）只能靠上面第 ② 条人工入库；
  没有原件就没有这条内容，不做转述、不做摘要替代。
- 「利率市场化 / 公开市场操作」这类**不含产业主题词**的央行稿子，规则抽取能入库、
  能 `verify`，但抽不出带板块映射的利好结论（没有主题词 → 映射不到通达信板块）。
  它们出现在「央行相关文档」计数与「其余信号」表里，不会强行塞进个股排序表。
- 央行**数值型数据**（M2、社融、LPR、公开市场操作量）目前完全不覆盖：现有链路只处理
  「可回溯到原文的政策文本」，数值面板是另一条线，暂无计划。

## 它是怎么跑的

```
sources.yaml 入口页
  → 适配器做链接发现（gov_json_feed：栏目页渲染用的静态 JSON；html_list：列表页正则；
    local_inbound：本地目录，不发网络请求）
  → 抓详情页，落原文快照 + 正文指纹去重
  → 规则抽取政策信号（无 LLM key 时默认路径；有 key 自动切 LLM，同一套校验兜底）
  → 按窗口筛 published_at（[目标日-1, 目标日]；--since N 才回补更早）
  → 主题归一（data/theme_alias.csv）→ 板块匹配（精确/别名/包含/模糊≥0.8）
  → 板块成分股展开 → 过滤（ST、市值<30亿、北交所）
  → 公司级相关性闸门（data/theme_business.csv × F10 主营/营收构成）
  → 信号聚成政策事件 → 打分排序（相关性主导）→ 全局 min_relevance 截断
  → 排序后取前 5 只（max_picks）→ 跨事件全局只剩前 5 只（global_max_picks）
  → Top-K 个股深度研读（需 LLM，无 key 跳过）→ 落库 → 出日报
```

### 抓取合规

- **限速**：单 host 串行，间隔由 `sources.yaml` 的 `rate_limit_seconds` 下发（当前全部 2.0s）。
- **UA**：显式声明 `PolicyPulse/0.1 (personal research)`。
- **robots.txt**：按站读取并遵守，判定分四档（见 `policypulse/http.py`）：

  | 档位 | 含义 | 行为 |
  |---|---|---|
  | `allowed` | 取到规则 | 按 Allow/Disallow 判定，被禁路径直接拒绝 |
  | `disallowed` | 规则明确禁止 | 不抓 |
  | `unavailable` | **拿到 HTTP 响应**但无可用规则（403 / 404 / 302 到 HTML / 内容不是 robots 格式） | 保守策略：仍抓公开页面，该 host 间隔抬到 `robots_unavailable_interval`（默认 5s），原因进日报告警区 |
  | `error` | **没拿到响应**（TLS 校验失败 / 连接失败 / 超时） | **fail-closed，不抓**，显式报错进告警区 |

  关键一点：`error` 绝不能退化成 `unavailable` 照抓——否则遇到真 `Disallow: /`
  的站会静默违规。robots 请求复用正文抓取那个 `httpx.Client`，不另起 `urllib`：
  两套 TLS 栈的 CA / 代理配置不同，同一台机器上可能一个通一个不通，会让档位静默翻转。
- 只抓公开页面，不绕登录 / 验证码 / 付费墙。

### 去重与当日口径

- 单次 run 内按正文 `content_hash` 去重；**跨 run 也去重**（指纹已在库则不再重复抽取），
  当天要重跑用 `--no-dedupe`。被跳过的条数写在日报概览里。
- **不再有「窗口内无更新就退回抓入口页最近几条」的兜底**（旧 `lookback_days: 14`
  就是「今日日报里混进 10-09 / 10-14 旧文」的根因）。窗口内没有新稿就是没有新稿：
  日报写「今日无新增政策 / 事件」，告警区不报错，CLI 退出码 0（抓取失败才非 0，
  好让 cron 看得见真故障）。

### 映射口径

概念板块成分由第三方（通达信）维护，宽口径，可能包含非核心主题个股；
本表是政策主题映射结果，不代表个股基本面判断。这句话固定出现在 Markdown「今日概览」
下方和 HTML 顶部，不只写在文档里。

### 反幻觉硬校验

- 每条信号的 `quote` **必须是原文子串**（归一化空白后比对），不通过则置信度压到 ≤0.4。
- 结论必须带 `source_url` + 原文片段 + `fetched_at`，缺一不产出。
- `policypulse verify` 重抓原文，报告「一致 / 内容已变 / 链接失效 / **被限速、暂不可验** / 抓取失败」。
  429 / 503 归到被限速，不算链接失效。原文指纹取**可见文本**，避开脚本注入的计数器噪声。

### 日报口径

**统计窗口**：`[目标日-1, 目标日]`（`daily_window_days: 2`）；`--since N` 时为
`[目标日-N, 目标日]`。窗口由 `published_at` 决定，概览第一行写明实际窗口。窗口外的稿子
既不抓也不入库进当天结论——这是「今日日报只收当日增量」的实现边界。

日报正文分四类呈现，读者按「能不能落到具体公司」逐级往下看：

- **受益个股**：政策明确指向具体行业，且深读判定「有实质影响」的事件；
- **主题关联**：政策与行业有关，但没有足够证据判断会实质影响公司业绩，只作主题参考；
- **待确认**：本期还没做出实质影响判断的事件，先列出可关联的主题与个股；
- **其他政策信号 / 泛政策表述**：方向中性或偏空、力度不足、没落到具体板块，以及整体性
  的方向性表述——留痕，不展开个股。

事件正文只收「利好 + 有板块映射 + `strength >= min_strength_for_report`」（默认 3）；
力度不够的信号落到「其他政策信号」里照样可见，完整结论与过滤原因在同日 JSON 与数据库，
不静默丢。

### 打分（score-v2）

`score = 0.25×力度 + 0.50×公司级相关性 + 0.10×市值流动性 + 0.05×板块受益面 + 0.10×政策性质`

权重集中在 `policypulse.yaml`（不存在则用 `Settings` 默认值）。变化点：**公司级相关性
成为主导项**，市值流动性从 0.20 降到 0.10 且只作并列决胜位；**去掉了「每板块 Top-N
（默认 5）」硬上限**，改为全局按 `min_relevance` 截断——排序是真正的从高到低，
不再是「先按板块分组、组内按市值排」。

### 选股上限 5 只（`max_picks` + `global_max_picks`，WAN-21）

**整份日报全局最多 5 只**，而且**先排序、再截断**，不是展示层裁剪：

- 信号级：`scoring.rank()` 打分 → 相关性截断 → `select_top(...)` 取前 `max_picks`（默认 5）；
- 事件级：`clustering.cluster_events()` 把同一事件内多信号推的个股按代码去重、
  按 score 降序排，再取前 `max_picks`；
- 全局级：`clustering.apply_global_pick_cap()` 跨事件按代码去重（同码只留 **score 最高**
  那次）、全局 score 降序排，再取前 `global_max_picks`（默认 5），并回写各事件 tickers——
  md / html / json 三端都走这一份结果，json 不会和日报脱钩；
- 排序键统一为 `(-score, -相关性, -市值, ticker.code)`，末位 `code` 升序做确定性
  tie-break：完全同分的两只不会随字典/集合顺序漂移；
- 候选不足上限时按实际数量返回，**不补位**；返回结果始终是 score 降序，
  可以直接核对「留下的就是最高的 5 只」；
- `score` 是政策受益度打分，本项目不产出概率数值，只把它当作相对上涨概率的排序代理；
- 可回退：`policypulse.yaml` 里 `global_max_picks: 0` 关闭全局截断，退回「每个政策事件
  各 `max_picks` 只」。`max_picks` 必须 ≥ 1（`global_max_picks` 的 0 是合法的关闭值）；
  旧键 `max_tickers_per_signal` 会被迁移，且不会把上限放大回 5 以上。

### 公司级相关性闸门（`policypulse/relevance.py` + `profile.py`）

老口径的「相关性」其实是**板块名命中**：「职业教育」能映射出大中矿业、永辉超市，
「新零售」会出现五粮液、京东方——它们只是通达信板块成员，跟政策没有业务交集。
闸门换成**公司业务命中**：

- `profile.py` 取通达信 F10 的「公司概况 + 主营构成」，解析出主营业务 / 经营范围 /
  研究行业 / 营收构成段，按 `company_profile_ttl_hours`（默认 7 天）缓存到 `data/cache/`。
- `relevance.py` 拿 `data/theme_business.csv` 里该主题的业务关键词去比对：
  主营业务 / 营收构成 / 研究行业命中 → **强命中**，放行并抬分（`+0.15`）；
  只有经营范围命中（关键词 ≥3 字，因为经营范围满是「技术培训」这类样板词）→
  **弱命中**，不放行，相关性压到 `0.45`；无交集 → 压到 `0.25`；拿不到画像 →
  放行并标注 `no_profile`（不能把「没数据」当成「不相关」）。
- 低于 `min_relevance`（默认 0.5）的个股不进日报正文，但**仍入库**（`gate_basis` /
  `gate_evidence` 两列），便于事后审计为什么被过滤。`relevance_gate_mode: drop`
  则直接不产出。
- 闸门是纯规则，**无 LLM key 也照常工作**；证据串列出全部命中关键词，可点回 F10 原文。
- `data/theme_business.csv` 与 `theme_alias.csv` 分工：别名管「主题 ↔ 板块名」，
  业务关键词管「主题 ↔ 公司主营」，后者刻意避开「消费」「装备」这类泛词。
  新增主题时两张表都要补，`tests/test_gate.py` 会检查覆盖。

### 政策事件聚类（`policypulse/clustering.py`）

同一文档 / 同一场发布会抽出的多条信号在报告里是重复的（旧产物里 68 条信号有成对
重复、个股表完全一样）。聚类按 `doc_id`（同文档）与 `title+org+published_at` 的
近邻（同发布会）归成一个「政策事件」，个股结论**按事件汇总去重**：同一股票在同事件下
只保留分最高那条。事件编号 `E{n}` 出现在报告里，信号层仍全部落库，不丢信息。

### 个股深度研读（`policypulse/deepdive.py`）

对全局 Top-K（`deepdive_top_k`，默认 3）个股产出结构化小报告：主营业务与相关营收占比、
政策传导路径（直接 / 间接 / 中性）、竞争地位、实施风险、个股特有风险。**需要 LLM**：

- 未配置 LLM → 整段跳过，日报里写明跳过原因，不阻塞其它环节（本 issue 边界 2）；
- **溯源红线**：模型返回的每条 `sources.ref` 只认三类白名单——该政策的 `source_url`、
  `通达信F10:<code>`、原文快照路径；出现其它 ref 或必填字段缺失 → 整段作废不产出。
  `llm_fn` 参数化，规则路径做不出带溯源的深度结论，宁可空着也不编。

## 没有 LLM key 也能跑

抽取默认是确定性的规则/词表，本机没有任何 LLM key 时全链路照常跑通。
配置下面任一环境变量后自动启用 LLM 抽取，并回退保护：

```bash
export POLICYPULSE_LLM_BASE_URL=https://api.openai.com/v1
export POLICYPULSE_LLM_API_KEY=sk-...
export POLICYPULSE_LLM_MODEL=qwen-plus
```

每次调用会落 `llm_calls` 表，模型或 prompt 变更时 `prompt_version` 递增。

## 加一个信息源

只改 `sources.yaml`，不写深链：

- 政府网栏目：找栏目页渲染用的静态 JSON，用 `gov_json_feed` + `feed_url`。
- 普通列表页：用 `html_list` + `link_pattern`（+ 可选 `reject_pattern`）。
- 转载源只想收某一类稿子：再加 `title_pattern`（对标题做正则过滤）。
- 站点 robots 禁止抓取、但你自己能下载：用 `local_inbound` + `inbound_dir`，
  见上一节「人工入库」。

`html_list` 的两个坑（WAN-26 实测踩过，配源时按这两条自查）：

1. **日期在哪**：href 里没有月日、日期只在列表项文本里（商务部
   `/art/2026/art_<uuid>.html`）时，适配器会从「这一条」列表项里取日期
   （`[2026-10-23]` / `2026年10月23日`），取不到才交给详情页的 `meta PubDate` 兜底。
   没有日期的候选会绕过列表级窗口过滤，只能靠详情页判定——既慢，也容易白抓旧稿。
2. **`max_items` 要覆盖整页**：栏目聚合页的页面顺序通常是「栏目顺序」而不是时间顺序
   （商务部 `xwfb` 首页先排领导人活动、再排部领导活动，产业类栏目排在后面）。
   `max_items` 取小了，被截掉的正好是排在后半段的产业稿件。窗口过滤与
   `max_docs_per_source` 截断现在都在候选按发布日期倒序之后才发生，
   但发现阶段就截断的条目救不回来。

新增源必须在 `sources.yaml` 里写明该栏目路径与 **robots 实测判定**；日报的
「信息源与合规」表会逐源列出判定结果与本轮文档数，停用源一律标注「不发起任何请求」。

## 测试

```bash
pytest                          # 离线单测（默认）：抽取/映射/打分/存储/报告
pytest -m network               # 真实抓取六个源，验证栏目没改版
pytest -m tongstock             # 校验本机 tongstock 数据可用
```

## 每天自动跑

**cron 的 PATH 坑（实测踩过）**：cron 环境的 `PATH` 通常是
`/usr/bin:/bin`，**不含 `~/.local/bin`**，于是 `tongstock` 找不到，
`policypulse run` 直接失败——手动在 shell 里能跑通不代表 cron 能跑通。两种修法任选：

1. crontab 顶部加 `PATH=` 环境行（推荐，见下例；**环境行对整表生效**）；
2. 或在 `policypulse.yaml` 里写绝对路径：`tongstock_bin: /Users/you/.local/bin/tongstock`
   （`selftest` 用的就是同一配置，先跑 `policypulse selftest` 确认）。

```cron
PATH=/Users/you/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
30 8 * * * cd /path/to/PolicyPulse && . .venv/bin/activate && mkdir -p logs && policypulse run >> logs/run.log 2>&1
```

cron 的默认 PATH 只有 `/usr/bin:/bin`，`~/.local/bin` 不在里面，`tongstock`
会报 `not found on PATH`——把本机 `command -v tongstock` 的真实路径写进上面第一行
（crontab 环境行对整表生效），路径写死，不要用 `~`（cron 里不展开）。

不动 crontab 的话，也可以把 tongstock 二进制绝对路径写进 `policypulse.yaml`：
`tongstock_bin: /Users/you/.local/bin/tongstock`，`TongstockClient` 优先用这个路径，
不依赖运行环境的 PATH。

注意两件事：

- 退出码：窗口内**没有新稿也是 0**，只有「抓取失败导致无产出」才是 1，
  所以 cron 的失败告警对应的是真故障，不是空日报；
- 日报按 `--date`（默认今天）落 `reports/YYYY-MM-DD.md`，同一天重跑会覆盖同名文件，
  跨 run 去重默认开着，重跑不会重复产出信号。

## 已知边界（MVP 有意不做）

- 源覆盖面：联网源 6 个（国务院×2 + 发改委/财政部/商务部 + 新华网财经转载），
  外加 1 个本地 inbound 源（人工下载的央行原稿）。工信部站点列表页是 jpaas 动态渲染，
  暂未接入。
- **央行**：pbc.gov.cn robots 全站禁止，直连源 `pbc_xw` 保持关闭；央行内容走
  「新华网转载 + 人工入库」两条合规路径，详见上文专节。
- 「利率市场化 / 公开市场操作」一类无产业主题词的央行稿子进得来、可 `verify`，
  但映射不到板块，不会出现在个股排序表里。
- 概念板块成员仍由第三方（通达信）宽口径维护，但个股要先过**公司级相关性闸门**才进正文；
  闸门靠 F10 文本关键词，是过滤器不是基本面判断，`no_profile` 时会放行（宁可漏杀不误杀）。
- 个股深度研读需要 LLM，无 key 时整段缺席；规则路径不伪造深度结论。
- 当日口径下日报明显变稀、空日报是常态——扩高频源（新华财经、政府要闻等日更栏目）
  另开 issue，不在本 issue 扩源。
- 打分是可解释的规则加权，不做价格预测、不给目标价、不接交易。
