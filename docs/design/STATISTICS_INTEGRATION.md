# 新界面接入现有统计数据 · DS-002

本页给负责新界面的设计GPT和前端开发者。视觉与布局可以重做，数据仍通过现有只读统计网关获得；不重新实现采集、直接调用寿司郎接口或读取运行中的SQLite。当前线上固定rc64，源码rc68的预测／经历模块尚未部署；以[完整交接](../PROJECT_HANDOVER.md)记录的实际部署为准。

## 接入顺序

1. 确定新界面的代码目录、入口和静态资源清单；视觉草稿／示例数据不直接替换线上页面。
2. 将门店、月份、日期、队列做成界面状态，接下方三类GET；筛门店与队列在客户端处理。
3. 对照同一门店、同一天的旧统计页，核对原前三位、时间、计数与缺口；切换选择后旧请求不得覆盖新选择。
4. 用无观测、接口失败、部分归档未加载、月底／跨日、手机宽度验证显示。
5. 同源发布新网页资源，保留现有API和原始归档；更新显示层不需要重建采集任务或重置期限／预算。发布前保留可恢复的旧网页和明确资源版本。

推荐沿用 `/statistics` 入口与同一服务器上的 `/api/v1/…`，从而继续使用固定统计地址。代码里的接口用相对路径；服务器地址是部署配置，不写死到公开代码或设计资产。

## 三类公开接口

| 界面需要 | 请求 | 返回如何使用 |
| --- | --- | --- |
| 当前月历／门店选择 | `GET /api/v1/days` | `store_names`是店号→名称，`configured_store_ids`为配置目录，`days[北京时间日期][店号]`为摘要；多店每日服务默认当前月 |
| 切换月份 | `GET /api/v1/months/YYYY-MM` | 明确月份的同结构摘要；检查响应`month`与当前选择相同 |
| 门店当天曲线／原号码明细 | `GET /api/v1/stores/{店号}/days/YYYY-MM-DD` | 检查`requested_store_id`、`local_date`；使用`summary`和`points` |

只查目录里已有的店号，不尝试未知ID。当前网关不支持查询参数，所以不要添加`?store=…`、`?timestamp=…`或缓存随机数；日期与店号已经在路径里。`GET`以外的方法包括`HEAD`会被拒绝；未知路径404、查询／编码路径400、读取暂不可用503。错误响应可能是纯文本，不要先强行按JSON解析。

公开入口没有独立的`/queue`、`/status`、`/history`、个人号单或管理接口。首页“最新展示号”从所选**今天**明细中最后一个有效`points[].queues`取得，保留观测时间；若最后一次观测失败，说明失败／陈旧，不能把更早成功号码包装为刚更新。查看历史日期时展示该日历史，不说是当前实时号码。

## 月历摘要字段

全国目录汇总返回的是紧凑摘要，不要求它包含单店当天的全部字段。当前稳定的`days[date][store]`字段为：

| 字段 | 含义 |
| --- | --- |
| `store_id`, `local_date` | 该摘要的门店及北京时间营业日 |
| `observations` | 已保存的观测组数，含失败／边界暂停 |
| `successful_pairs` | 两个查询均成功的完整组数 |
| `failed_pairs`, `scheduled_pause_slots` | 失败与营业边界暂停分别计数 |
| `expected_background_slots_so_far` | 按声明营业时间，截至此时应完成的背景采样格 |
| `observed_background_slots` | 有成功队列响应覆盖的背景格 |
| `observed_slot_fraction_so_far` | 截至此时的覆盖比例；null保持未知 |
| `last_observation_at` | 最后保存的操作开始时刻；不是最新成功响应接收时刻 |

顶层`unavailable_store_ids`表示不可用／尚未加载等门店，不补零。若存在`calendar_index_state`、`calendar_pending_store_ids`、`calendar_verified_store_count`，先展示已完成数据和加载状态；不能把部分结果说成整个目录已经检查完。`collector_failed_store_ids`、`collector_process_state`及`origin_halted`仅描述已有服务状态，前端不能据此启动或重启写者。

月历颜色当前仍按**采集覆盖**，不按人流量。全部门店的覆盖按“已观测格总数÷预期格总数”，不能简单平均各店百分比；配置目录变化也不能把两天总观测组差当客流增长。日历需要多少格按真实月份计算，无数据与尚未加载分开。

## 曲线和号码字段

单店详情的`points`按保存顺序给出：

| 字段 | 用途与限制 |
| --- | --- |
| `operation_started_at` | 操作开始时间 |
| `queue_received_at`, `count_received_at` | 两个查询分别的响应接收时间；图中号码横轴用前者，数量横轴用后者 |
| `queues.mixedQueue` | 堂食原顺序前三个展示号码；字符串及前导零保留 |
| `queues.reservationQueue` | 预约原顺序前三个展示号码；不与堂食相减 |
| `call_reference_labels` | 原列表第一位的用户约定参考，不能认证实际已叫号 |
| `count_raw` | 原数量，null未知，单位尚未核实，不直接称现场等待桌数 |
| `pair_ok`, `scheduled_pause`, `error_codes` | 完整成功、边界暂停、失败的区别；`queues=null`和有效空列表不同 |
| `comparison_state`, `interval_seconds`, `removed_labels` | 展示集合的变化与比较间隔；只在`comparable_display_sets`和正间隔时计算变化速度 |

号码—时间图纵轴可以画第一位的纯数字位置，非纯数字号码仍在明细按原文显示。点之间遇失败、长缺口、运行边界或参考倒退应断开，不自动补造中间号码与时间。`storeQueue`可能含预约，不用它替代堂食。

若画展示变化速度，可用`removed_labels[队列] × 60 / interval_seconds`，单位为**展示号码变化／分钟**。若另画首位位置的推进速度，应另起名称、只比较同周期可比观测，不能与展示集合移出数量混成一种指标。五分钟／十五分钟图表只能称观察到的展示变化量；目前没有真实叫号量、真实人数或过号率接口，不把这些图标为已核实人流量。

详情最多返回2048个图点并受2MiB响应边界约束，检查`graph_truncated`和`returned_graph_points`；完整原始数组仍在数据库。原页面最近60行仅是显示策略，新页面可选择更多返回的点，但不能由此宣称已获得数据库的完整全天原始记录。

## 刷新、时间和错误处理

页面每30秒读一次已保存副本即可；同店多人查看不会触发更多寿司郎查询。先读月份，再按当前选择读一天；没有选择门店时不同时请求147家当天明细。历史月份／过去日可缓存；需要加载进度时继续读取索引。不要并发堆积同一请求，读失败保留明确失效状态，退避后重试；隐藏页面可以降低频率。

```js
// 同源网页中使用；storeId应来自刚读取的配置目录。
async function readStatistics(path, signal) {
  const response = await fetch(path, {cache: 'no-store', signal});
  if (!response.ok) throw new Error(`统计读取失败：${response.status}`);
  return response.json();
}
// const index = await readStatistics('/api/v1/months/2026-10', signal);
// const detail = await readStatistics(`/api/v1/stores/${storeId}/days/${date}`, signal);
```

这是读取示意，实际组件还须有超时、取消及选择版本保护：月份／店铺／日期改变时取消旧请求，同时比较请求时捕获的选择版本；即使取消没有及时生效，迟到响应仍不能覆盖新选择。清理定时器与监听器，避免组件重新进入后重复轮询。

后端日期是北京时间日期，时间戳内部UTC；界面统一用`Asia/Shanghai`转换，不直接用UTC的`toISOString().slice(0,10)`当今天。只显示页面更新时间还不够，号码旁保留响应时间。预测模块使用自己的新鲜度／失效规则，设计不覆盖它的判定。

节假日／调休标签可以依托已有`src/sushiwait/data/cn-mainland-2026.json`及`calendar.py`，打包明确年份和来源；当前公开统计接口没有独立节假日路由，未提供年份保持待核。日历里的“调休工作日”标签与用户声明的营业时间规则分别显示，不自行改变采集计划。

## 同源开发与发布

从别的端口、域名或本地文件直接请求当前服务器会受浏览器跨域限制；当前网关没有提供跨域授权。开发时由前端开发服务把固定`/api/v1/`转发到配置中的统计网关，生产时网页与统计接口放在同一来源；不为了设计改成任意域名均能访问或把管理端口公开。HTTPS网页也不能直接混用HTTP接口，正式HTTPS部署须同源一起配置。

现有网关只放行固定网页与静态资源路径。新设计若仍用`statistics.html/.js/.css`，要么保持现有DOM约定复用旧JS，要么重新写渲染层按上述契约接数据；HTML改变而直接沿用按旧ID查找的JS会报错。若用构建工具产生多个带hash的JS／CSS文件，需提供完整资源清单并增加明确的静态路径映射，不能把整个项目目录当公共文件服务。

当前内容策略只允许本来源脚本、样式及网络连接，禁止iframe嵌入。字体、图表库、图片在正式页面中应作为已核许可的本地资源打包；原型CDN链接、内联脚本／样式或外部字体未必能在现有网关运行。若原型只是外部托管页面或可视化文件，先取得可部署的HTML／CSS／JS或完整源码，再接数据。

## 与预测／私人记录的边界

新统计外观不能默认把号码、取号时间、经历或行程发送到只读API；不放URL、访问日志或公共文件。现有`reference-model.js`、`experience-model.js`与`experience-ui.js`可以另行复用，但其输入状态、门店固定／未保存提醒等约定必须保留，按[DS-001](CONTENT_SPEC.md)逐项接线。rc68理想时间算法只是后台研究工具，不能设计成已经在线可执行的自动取号按钮。

目前源码契约入口：`dailyview.py`、`dailyfleet.py`、`monitorhub.py`、`deploy/statistics_gateway.py`；旧渲染参考`web/statistics.js`。新界面交付时提供：代码目录／入口、资源清单、接口适配层、支持的状态、与旧页同数据的对照结果和未接功能。主线负责接口与服务器部署，设计任务负责视觉及界面代码，避免同时重写采集后台。
