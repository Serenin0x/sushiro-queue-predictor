<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/sushiwait-logo-dark.svg">
    <img src="assets/sushiwait-logo-light.svg" alt="SUSHIWAIT" width="840" height="308">
  </picture>
</h1>

<p align="center"><strong>少一点盯号，多一点自由安排。</strong></p>

想吃寿司，却不知道该几点取号、还要等多久、会不会突然过号？SUSHIWAIT 希望把这些不确定，变成有依据的用餐计划：结合寿司郎小程序的实时排队数据、历史规律和日期差异，帮助你安排取号与到店时间，把时间留给自己喜欢的事。

这是面向中国大陆寿司郎门店的独立开源项目。**实时接入有了新进展：除已走通的正常微信凭证查询链路，电脑还在三家试点取得了无需微信凭证的匿名排队数据。新工具把五类展示号码、原始数量和每次查询时间分别留住；三店每30秒、各30轮，共90份成对观测、180次请求全部成功，号码确实出现变化。三店随后完成两小时、720次请求全部成功。匿名监控工具将同店需求合并，已实际验证60秒转30秒刷新、八次请求全部成功；新增任务进度保存和显式中断恢复，已完成的结果不重查，未知请求保留为缺口。22项恢复专项与828项完整软件检查通过，独立安装和真实中断恢复均通过：三份成对观测、六次请求成功，已经保存的结果没有重复查询。原午间长采样已完成三店共360次成功，凭证更新缺口约143与127秒如实保留。现有本机展示出口可汇总号码、刷新结果与任务状态；新增本机只读号码服务，采集进行时也能读取已保存的号码，多人查看共用一份采集，失败与旧数据分别显示。22项专项与850项完整检查通过，安装包实际HTTP联动已验：后台采集时五次读取没有额外查询寿司郎，三份观测/六次请求全部成功；新增持久共享采集窗口，普通时段持续采集背景数据，临近用餐按同店最紧迫需求调整节奏；原截止时间和总预算跨重启保持，已保存的观测不重查。28项窗口专项与878项完整检查通过；隔离安装与真实恢复已验：三对六次请求全部成功，恢复后约60秒转30秒，五次号码读取没有额外上游请求。全天稳定性继续验证。新增匿名数据的日期与短时变化分析：五类展示号码和原始数量各按自己的响应时间比较，失败、重启与长缺口分别断开，资料不足明确显示。22项分析专项与900项完整检查通过，隔离安装和三份真实记录的三个时点回放通过，原库不变、额外请求为零；重启边界没有拼成连续叫号。新采集另记程序首次收到完整观测的时间，补录数据不会提前进入所选历史时刻；旧数据保留未知，为后续回测减少时间误用。20项接收专项与920项完整检查通过。新增 Linux 容器部署配置与安全重启选择，默认只向宿主本机发布号码服务；初镜像真实查询与合成重建分别通过，最终镜像按交接记录核对。新增手动云端连通性检查，固定三家试点、每店一次，公开结果只含成功与失败统计；源新鲜度和全天稳定性继续单独验证。新增终态窗口质量报告，完整核对记录链、逐店日期分布与实际缺口，预算提前结束也如实保留。新增真实经历的人工审核声明，确认、拒绝、证据不足与纠错分别留存，旧审核不会沿用到改过的结果。预测模型、用户页面和自动排队继续按真实数据与验收条件推进。**

## 产品与模型功能

### 当前可以做什么

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/5adc969d27a36489-dark.svg"><img src="assets/readme-labels/5adc969d27a36489-light.svg" alt="读到真实展示号码" width="130" height="20"></picture></strong>：从官方微信小程序确认实际查询地址，电脑已独立读取中关村、西单、成都世豪的堂食、预约展示队列。用户退出小程序后，后续电脑查询仍成功；完整保留号码、顺序和后缀。 新增[匿名排队来源](docs/ANONYMOUS_QUEUES.md)，电脑直接查询五类展示数组和原始数量，保留顺序与失败，三店30秒/30轮、180次请求全成功；长期稳定性继续验证。 新增[门店展示出口](docs/STORE_VIEW.md)，一并查看堂食与预约号码、最近成功响应时间和刷新结果；旧信息、失败、缺失分别显示，后台进程存活状态保留未知。 新增[匿名号码服务](docs/ANONYMOUS_SERVICE.md)，后台采集时也可读取五类已保存的展示号码；页面读取不增加上游请求，工作者是否运行与旧数据分别展示。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/bb36ec0d24eb180b-dark.svg"><img src="assets/readme-labels/bb36ec0d24eb180b-light.svg" alt="找到试点门店，再用电脑采集" width="210" height="20"></picture></strong>：目录接口已通过电脑查询，返回 147 条门店记录。保留三店 60 秒和 30 秒的短窗结果，保留 30 秒各 67 轮、主库 219 份成功快照的历史结果；最新新凭证三店独立验证成功，另库西单 120 次成功及一次跨到期恢复已完成，又实测保存任务进度后跨进程续采；最新另一个三店任务跨正常更新后各完成 120 次。全天稳定访问与全国完整覆盖仍待验证。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/9660e24eddc316c0-dark.svg"><img src="assets/readme-labels/9660e24eddc316c0-light.svg" alt="查看查询凭证的到期声明" width="178" height="20"></picture></strong>：直接检查本机私有配置的到期声明、剩余时长与更新版本，显示是否触发到期前保护；不输出凭证内容。已观察到正常初始化返回新凭证，独立自动更新仍在验证中。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/c886e589de78387b-dark.svg"><img src="assets/readme-labels/c886e589de78387b-light.svg" alt="把一次正常刷新接到电脑查询" width="210" height="20"></picture></strong>：可离线检查本人导出的捕获文件，明确选择正常单店请求，将完整上下文写入私有配置。到期或临近到期时拒绝导入；此功能不自动登录或续期。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/18e6edb814fdbffe-dark.svg"><img src="assets/readme-labels/18e6edb814fdbffe-light.svg" alt="为正常刷新准备更轻的接入方式" width="226" height="20"></picture></strong>：保留同电脑响应接收与安全诊断，新增正常目录查询的短时摘要接入工具。目录来源已实测取得完整新凭证，关闭小程序后电脑查询三店成功，无需导出 HAR；接入工具与同进程恢复已有重复实证；新增独立的短窗关闭保护工具，到时关闭调试并核对状态；新增私有暂存提交组件，调试关闭后才更新采集上下文，已实测公开组件完成主配置更新及原进程恢复；新增一个短窗协调入口，将暂存接收与独立关闭保护联动，已实测接入新凭证、提前关闭调试并恢复原进程；已记录单店 98.760 秒、旧三店约 179 秒、营业短采约 102 秒及午间长采约 143 秒更新缺口，继续改进更新速度。重复可靠性和长期自动更新继续验证。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/115f5d28279edda3-dark.svg"><img src="assets/readme-labels/115f5d28279edda3-light.svg" alt="凭证到期，采集有序暂停与恢复" width="226" height="20"></picture></strong>：在声明到期前 30 秒保护停采；可选择短时等待正常新凭证，通过检查后继续采集，并保持请求节奏。已实测保护暂停、等待超时停止，以及正常新凭证更新后无需重启的恢复。新增私有有界任务进度与显式重启续采，对账已保存结果、记录未知缺口，并跨重启检查凭证版本；西单已实测中断后续采。单店保留原采样目标；多店新增完整轮次的恢复目标与间隔保护，减少额外等待。已安装单店版的一次真实跨更新间隔为 98.760 秒，恢复后约 30 秒；多店新版真实验收另记；更新缺口和长期稳定性继续验证。长期无人值守与服务器独立更新仍待验证。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/139eaa95f99193d3-dark.svg"><img src="assets/readme-labels/139eaa95f99193d3-light.svg" alt="把数据留住，也把来源分清" width="194" height="20"></picture></strong>：使用 SQLite 保存公共快照，隔离新旧接口及真实、合成数据；保留字段缺失情况，支持历史变化与采集质量报告。新增公共字段分页导出，将允许的门店信息保存为私有数据包，保留缺失信息、稳定观测标识与内容校验，为后续服务器接收准备资料。新增包格式校验与本机事务归档，重复不新增、同标识不同内容整批停止；新增有容量上限的持久待确认目录，重开后仍能校验已保存的包，相同包重复加入保持原文件；新增有界本机接收服务，独立校验身份、验证整包并事务归档，成功后返回对应确认；新增本机投递、回执签名与逐条对应校验，保存后可以离线复核，原包始终保留；新增[采集统计页](docs/COLLECTION_MONITOR.md)：查看门店原始数量、堂食与预约展示变化及实际响应间隔，失败和断采缺口清楚标记；页面查看不额外查询上游；新增[六店分批采集](docs/STORE_EXPANSION.md)，上海、广州、深圳三店首次6次独立查询均成功，48小时统计服务正在积累观测，原三日试点保持。新增[Linux 部署说明](docs/LINUX_DEPLOYMENT.md)，打包匿名采集与号码服务；新增[手动云端检查](docs/CLOUD_SOURCE_CHECK.md)，核对云端 Linux 的当次来源连通性，实际服务器与远程上传继续验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/bf4fbb34b32ed56d-dark.svg"><img src="assets/readme-labels/bf4fbb34b32ed56d-light.svg" alt="看清采集是否可靠" width="130" height="20"></picture></strong>：报告区分请求失败、字段处理失败与本机停采，统计实际请求间隔、展示号码变化和字段状态。明确统计窗口，帮助发现异常；新增[终态窗口质量](docs/ANONYMOUS_WINDOW_QUALITY.md)，完整核对结果链、跨日期统计和成功请求缺口，区分预算用完与原截止结束；相同数据不会被当成刚更新的数据；可离线查看私有任务的成功、失败与未知槽位，不读取凭证或查询门店。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/fce6b943bcea13b8-dark.svg"><img src="assets/readme-labels/fce6b943bcea13b8-light.svg" alt="比较最近的展示号变化" width="162" height="20"></picture></strong>：按指定历史时刻分析两分钟或更长窗口，比较前后两段展示集合的变化速率；遇到失败、缺口和时间逆序会断开比较，资料不足会明确显示。新增[匿名响应窗口分析](docs/ANONYMOUS_SIGNALS.md)，当前无需微信凭证来源也能结合日期查看五类展示集合变化、独立数量差值和实际覆盖；两条响应、失败与缺口分别处理，未来数据不提前使用。新数据支持[本机首次接收筛选](docs/ANONYMOUS_INTAKE.md)：后来补录的观测不会被算进早先历史窗口，旧记录缺失接收信息时明确保留未知。为异常检测准备可观察指标，尚不判断真实过号人数或触发提醒。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/65105f86981488eb-dark.svg"><img src="assets/readme-labels/65105f86981488eb-light.svg" alt="看懂页面数字的含义" width="146" height="20"></picture></strong>：已核对堂食与预约展示数组，以及“已签到等待桌数”的客户端字段绑定和西单同次响应数值。列表与详情分别记录刷新情况，营业状态显示关闭时也可能有叫号。页面更新时间与门店源时间分开解释，避免把刷新动作当作数据刚刚更新。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/d0c5c2c9d3a688e2-dark.svg"><img src="assets/readme-labels/d0c5c2c9d3a688e2-light.svg" alt="把已有排队经历留作预测依据" width="210" height="20"></picture></strong>：本机可以校验和保存自己记录的取号、签到、叫号、过号、取消与入座结果，保留不确定时间范围和完整修订历史；新增只读历史回放，核对整条修订链并按声明的历史时刻选择结果，避免后来纠错覆盖早先版本。人工记录与合成样例分别统计；新增私有区间误差计算，可核对已记录预测与叫号区间的误差范围、覆盖情况，保留不确定性。新增[首次接收账本](docs/OUTCOME_INTAKE.md)，由程序单独记录收到每份修订的时刻，补录的旧经历不会被提前算入历史回放；重复提交保留原接收时间。新增[人工审核记录](docs/OUTCOME_REVIEWS.md)，为具体结果生成未批准草稿，保存人工结论及其接收时间；结果改过后须重新审核，晚到审核不会倒灌历史，冲突保持未确认。新增[历史区间研究基线](docs/HISTORY_BASELINE.md)，按同店、队列和日期匹配经历，计算新取号等待、已排队剩余等待和理想时间候选；保留上下界，资料不足明确缺失。新增[时间顺序检验](docs/BASELINE_BACKTEST.md)，每个历史时刻的训练只用此前已接收的资料，核对之后叫号区间的误差范围；保留冷启动和不同提前量。人工确认与事件真实性认证分开，回放不是当时真实预测日志，模型还需真实结果校准。 新增[本人排队号查询](docs/PERSONAL_QUEUE_ACCESS.md)，独立读取自己的号码、签到状态、人数与桌型，可一并保存状态历史；个人凭证和号单单独私有保存。缺少前方桌数或真实叫号事件时明确保留未知，不用号码差冒充精确排位。 新增[经历与门店观测对齐](docs/OUTCOME_FEATURES.md)，将审核过的排队过程与预测当时已接收的队列变化、日期和数据质量组合为私有研究资料；后来补录不会提前进入特征，缺失保持可见。 新增[候选分布与AI权重融合](docs/DISTRIBUTION_FUSION.md)，保留上下界并重新计算分位点，过期或无效建议回退；公共趋势摘要不包含本人号码，尚未调用供应商或开放实时预测。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/62455254a15761ec-dark.svg"><img src="assets/readme-labels/62455254a15761ec-light.svg" alt="让每一天有正确的日期身份" width="194" height="20"></picture></strong>：离线识别 2026 年普通工作日、普通周末、节假日与调休上班日，保留星期、月份和日历季节。历史回放会核对公告当时是否已发布，其他年份保留未知，为后续预测准备可靠日期依据。新增离线用餐时间策略，核对叫号偏移和临近用餐的 60／30 秒刷新目标；还能离线合并同店关注需求，按更紧迫的计划确定节奏，并及时重算窗口边界。新增有界自适应采集，按实际开始时间运行同店共享的 60／30 秒查询，并在窗口边界重算；已用安装包实测 60 秒切换到 30 秒、四次查询成功；这是闭店调度验证；新匿名来源已实际验证同店共享的60／30秒策略，四份成对查询/八次请求成功；固定周期采集新增[任务保存与恢复](docs/ANONYMOUS_TASKS.md)，未知槽位不重发，原预算不重置。新增[持久共享窗口](docs/ANONYMOUS_WINDOWS.md)，将背景采集与同店60／30秒计划一并保存，重启保持原期限和预算，未知结果留为缺口；长期实际稳定性与提醒继续验证。 新增[运行中计划更新](docs/MONITORING_PLAN_UPDATES.md)，用户计划可通过显式私有入口整组调整；后台重新合并同店60／30秒需求，原期限和预算保持，旧任务不升级；多用户入口与提醒继续开发。 新增[部署计划管理](docs/LINUX_PLAN_UPDATES.md)，运行中修改整组关注计划或恢复背景采集，自动生成版本；独立卷隔离计划与观测，后台是否接受可单独核对。 新增[多日采集计划](docs/MULTI_DAY_COLLECTION.md)，按明确天数自动接续正常窗口；跨日和重启保留同一总截止、预算与同店间隔，出现未知请求或失败如实停止，历史资料分段保留。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/5d2490ac8d59802b-dark.svg"><img src="assets/readme-labels/5d2490ac8d59802b-light.svg" alt="在上线前检查数据处理" width="162" height="20"></picture></strong>：提供合成回放、1233 项本机代码检查、安装包验证及 GitHub 离线检查流程，覆盖号码保存、错误处理、来源隔离和旧数据库兼容；自动检查结果与真实接口验收分别记录。

跨到期恢复的重复可靠性、部分字段单位、门店源时间、允许长期频率和全国覆盖仍未验收。目前的工具用于接入验证，预测能力尚未开放；有限展示号码的变化不会被当成真实过号率。

### 正在奔向的用餐体验

以下是产品目标，尚未上线；每项实现并验证后，会同步更新这里的状态。核心 v1 优先由用户输入已有号码进行实时预测和提醒；自动取号、取消和重排保留为后续阶段。

<picture>
  <source media="(prefers-color-scheme: dark) and (max-width: 600px)" srcset="assets/sushiwait-experiences-dark-mobile.svg">
  <source media="(max-width: 600px)" srcset="assets/sushiwait-experiences-light-mobile.svg">
  <source media="(prefers-color-scheme: dark)" srcset="assets/sushiwait-experiences-dark.svg">
  <img src="assets/sushiwait-experiences-light.svg" width="840" alt="六项规划体验：取号心里有数——预计叫号时间，提供预测区间，让到店安排有依据。；提前规划用餐——输入到店时间与等待偏好，获得建议取号时机。；叫号一处掌握——同看堂食与预约展示号，掌握更新时间和刷新状态。；临近用餐提醒——临近用餐更频繁地更新，及时提醒到店与过号风险。；跳号及时调整——结合日期规律与号码变化，及时修正预测。；按计划取号——授权和规则验证后取号、重排，必要时提醒提前到店。">
</picture>

<details>
<summary>看看时间偏好、刷新节奏与重排规则</summary>

- **时间偏好：** <code>+10</code> 表示到店后希望等约 10 分钟；<code>-10</code> 表示希望提前 10 分钟叫号，结合门店规则评估风险。
- **刷新节奏：** 用餐前 30 分钟按 60 秒、前 15 分钟按 30 秒的目标节奏更新；异常加速时提前重算，给出过号风险与到店提醒。
- **预测依据：** 具体星期、工作日、周末、节假日、午晚时段和月份季节趋势；真实过号率需额外事件证据。
- **重排边界：** 取号、取消和重排须分别验证授权与规则；若重排会明显过晚，提醒提前到店。
- **叫号展示：** 未来在本项目小程序中同步堂食与预约展示号、更新时间和刷新状态，减少两个小程序之间来回切换。

</details>

目标是逐步支持大陆任意门店；每家店的实时数据、预测和操作能力将分别标明验证状态。预测误差通过真实结果校准，有限的展示号码不会被当作完整队列。

## 技术手段

当前使用 Python 3.11+ 标准库、固定 HTTPS 只读请求和 SQLite；新增独立匿名CRM采集、报告及按私人计划的共享60／30秒调度，和微信凭证来源隔离，并新增[匿名任务保存与显式恢复](docs/ANONYMOUS_TASKS.md)，见[匿名来源说明](docs/ANONYMOUS_QUEUES.md)。保留分队列展示数组、字段存在状态与数据来源。通过有界离线捕获检查、同电脑短时上下文接收与正常目录摘要接入、私有上下文原子更新、到期保护与可选等待恢复完善采集可靠性。运行、凭证状态及批量采样步骤见 [数据验证手册](docs/DATA_ACCESS.md)，正常更新证据与进入条件见 [凭证更新说明](docs/AUTH_REFRESH.md)，辅助端与采集恢复见 [本机接入说明](docs/BRIDGE.md)，无 HAR 路线见 [正常查询接入](docs/SURGE_INTAKE.md)，客户端辅助采集与服务器保存的实施计划见 [长期采集方案](docs/LONG_TERM_COLLECTION.md)，持久有界任务和重启对账见 [任务恢复说明](docs/COLLECTION_TASKS.md)，独立关闭保护与实际限制见 [短窗保护说明](docs/SURGE_GUARD.md)，完整暂存提交见 [更新协调说明](docs/CONTEXT_PROMOTION.md)，单店恢复节奏见 [恢复周期说明](docs/RECOVERY_CADENCE.md)，短窗联动与前置条件见 [窗口协调说明](docs/CONTEXT_WINDOW.md)。 普通采集的[有限临时错误处理](docs/TRANSIENT_SAMPLING.md)可保留部分失败并继续后续槽位，默认首错停止，鉴权失败仍立即停采。

字段解释见 [字段映射](docs/FIELD_MAPPING.md)，真实采样与剩余条件见 [接入验收报告](docs/V0_2_ACCEPTANCE.md)，自动检查与安装验证见 [检查说明](docs/CHECKS.md)。缓存、HTTP 时间和配额提示保留为单独观察，运行资料默认存放在本机应用数据目录；本机公共字段导出、分页和保存状态见[导出说明](docs/PUBLIC_PACKETS.md)，接收格式校验与本机去重/冲突规则见[归档说明](docs/PACKET_ARCHIVE.md)，持久待确认目录与容量/故障规则见[保存说明](docs/PENDING_PACKETS.md)，本机接收与确认协议见[服务说明](docs/PACKET_RECEIVER.md)，本机投递与签名确认保存见[投递说明](docs/PACKET_DELIVERY.md)。结果记录、私有追加库与操作边界见[结果记录说明](docs/OUTCOMES.md)，日期/时段分类及历史信息规则见[日期特征说明](docs/CALENDAR.md)；记录中的误差范围与覆盖计算见[区间评估说明](docs/INTERVAL_EVALUATION.md)，完整修订核对与历史声明回放见[结果回放说明](docs/OUTCOME_COHORT.md)，独立首次接收库与按本机接收时间回放见[接收说明](docs/OUTCOME_INTAKE.md)。 人工结论、版本绑定与审核接收时刻见[审核说明](docs/OUTCOME_REVIEWS.md)，分层历史、条件剩余等待与理想时刻候选见[历史基线](docs/HISTORY_BASELINE.md)，时间顺序回放及结果算术见[基线检验](docs/BASELINE_BACKTEST.md)。

展示集合的窗口统计、历史时刻过滤与样本不足规则见[窗口分析说明](docs/SIGNALS.md)；该工具只读已有快照，尚未接入预测、告警或自动排队。用餐偏移与近时段刷新目标见[监控时间策略](docs/MONITORING_TARGETS.md)，离线工具保留；[同店共享策略](docs/SHARED_MONITORING.md)会合并请求目标，并在时间窗口切换前安排重新判断。[自适应采集](docs/ADAPTIVE_COLLECTION.md)将策略接入一个进程内的有界只读查询，遇到凭证保护或首个失败停止；尚无常驻调度或提醒。 门店号码与最后刷新结果的只读投影见[门店展示说明](docs/STORE_VIEW.md)；响应年龄与源更新时间、后台进程存活分别解释；可显式关联同库私有任务，停止或过时状态让各店展示同步转为最后已知信息。

匿名号码服务使用原创ASGI应用、一个数据库所属工作者和标准库展示投影；可选server安装项采用Uvicorn提供本机HTTP，基本数据工具仍无运行依赖。后续规划使用 PostgreSQL、Redis 后台调度及统计与分位数预测。规划采用[历史初估与实时融合模型](docs/REALTIME_PREDICTION_MODEL.md)，已实现[候选分布算术和公共权重契约](docs/DISTRIBUTION_FUSION.md)，大语言模型将参与状态判断和受限数值更新，也处理有来源的外部事件；DeepSeek为优先，[低成本开放模型候选](docs/LLM_MODEL_CHOICES.md)可替换，其实际调用和收益仍待实现与回测验证；PostgreSQL、Redis 与数值预测模型尚未实现。

前端在实时数据准备充分后开发。Logo 已选定 Unbounded 的红色像素刷痕矢量版，随 GitHub 浅色／深色模式自动切换；设计与字体来源见 [Logo 说明](docs/BRAND_DESIGN.md)，README 横幅与后续动态图文继续按独立视觉任务推进。

## 版本说明

当前源码候选为 **v0.2.0rc46**，新增[实时公共特征与分布融合](docs/DISTRIBUTION_FUSION.md)：按有效情景权重重新求等待分位区间，检查过期、算术和输入版本；只读趋势接口不增加门店查询。33 项新增检查、完整 1233 项本机检查通过；新安装、镜像和公开结果单独记录。此前[采集统计页](docs/COLLECTION_MONITOR.md)的安装、浏览器和公开CI证据保留在交接历史。按最新[核心版范围](docs/V1_CORE_SCOPE.md)，v1 优先手动输入自己的号码进行实时预测和提醒，精确个人前方桌数与自动取号、取消、重排后置；预测与误差仍需真实结果校准。正式 v0.2.0 和 v1.0.0 尚未完成。

## 更新说明

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/edb5c7791184fd16-dark.svg"><img src="assets/readme-labels/edb5c7791184fd16-light.svg" alt="2026-10-08 · v0.2.0rc46" width="179" height="20"></picture></strong>：新增公共实时趋势入口和候选分布融合，重新计算区间分位点；AI建议绑定观测、时间和模型版本，无效或过期回退，新输入拒绝旧结果。供应商调用、私人号码追踪和实际误差仍待验。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/feeb0b014d31ac09-dark.svg"><img src="assets/readme-labels/feeb0b014d31ac09-light.svg" alt="2026-10-08 · v0.2.0rc45" width="179" height="20"></picture></strong>：新增采集统计页，让原始数量、堂食与预约展示变化、实际响应间隔可见；失败不补零、缺口断开曲线、停止保留历史。新增上海、广州、深圳48小时分批采集；核心v1采用手动号码实时预测路线，自动业务后置。补充历史初估、实时趋势与大模型数值融合设计及官方费用／许可研究，尚未调用或校准。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/d96820671eda0ba8-dark.svg"><img src="assets/readme-labels/d96820671eda0ba8-light.svg" alt="2026-10-08 · v0.2.0rc44" width="179" height="20"></picture></strong>：让已审核的排队经历与当时门店观测对应起来，生成私有研究资料；后来收到的旧响应不倒灌历史，有限号码变化和未知单位计数分别保存。模型仍需真实叫号结果校准。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/a520f3ba2002dc14-dark.svg"><img src="assets/readme-labels/a520f3ba2002dc14-light.svg" alt="2026-10-07 · v0.2.0rc43" width="179" height="20"></picture></strong>：把“我的号单”接入预测资料准备：新增当前个人票据与状态历史只读查询，号码、签到、人数和桌型只入私有文件，失效会话提前停止。前方桌数、历史事件含义与新取号位置分别验证，避免仅凭号码差误判等待时间。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/e5b0509aa9c5b754-dark.svg"><img src="assets/readme-labels/e5b0509aa9c5b754-light.svg" alt="2026-10-07 · v0.2.0rc42" width="179" height="20"></picture></strong>：让历史按计划持续积累：新增多日采集入口，正常窗口自动接续，跨日和重启保留总期限、预算与同店节奏；原始资料分段保存，失败与未知请求留有依据。展示共用后台结果，真实多日稳定性与预测继续验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/698241e6fdfbb421-dark.svg"><img src="assets/readme-labels/698241e6fdfbb421-light.svg" alt="2026-10-07 · v0.2.0rc41" width="179" height="20"></picture></strong>：时间改了，后台计划也能一起改：Linux部署新增独立计划卷和管理入口，自动生成更新版本，同店多人共享采集。清空计划恢复背景节奏，原期限与预算保持；文件已发布和后台已接受分别显示。容器验证使用合成来源，真实预测、通知和生产部署继续验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/886bdfa11b63c899-dark.svg"><img src="assets/readme-labels/886bdfa11b63c899-light.svg" alt="2026-10-07 · v0.2.0rc40" width="179" height="20"></picture></strong>：计划变了，监控跟着调整：新增显式计划更新入口，让运行中的后台接受新时间和关注状态，同店共用查询，临近用餐切换60／30秒节奏。新版本有据可查，原截止和请求预算保持；独立安装版真实查询已验证60秒转30秒，展示读取共用后台采集。实际预测与提醒继续验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/05f144782e048efa-dark.svg"><img src="assets/readme-labels/05f144782e048efa-light.svg" alt="2026-10-07 · v0.2.0rc39" width="179" height="20"></picture></strong>：检验历史，也尊重当时的信息：每个取号或已等待时刻重新筛选已收到的经历和审核，再计算后续叫号误差；后来补录不提前训练，资料不足也留下记录。按不同提前量分别统计，回放与真实模型成绩分开。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/863ef543701efe87-dark.svg"><img src="assets/readme-labels/863ef543701efe87-light.svg" alt="2026-10-07 · v0.2.0rc38" width="179" height="20"></picture></strong>：让历史经历开始参与计算：按门店、队列、日型和时段寻找参考，已等待时重新估计剩余分布，理想时间逐个比较候选；保留区间、不假设取号时间单调，没有足够资料就明确缺失。研究候选与真实校准成绩分别记录。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/a07a99db4f7bdb68-dark.svg"><img src="assets/readme-labels/a07a99db4f7bdb68-light.svg" alt="2026-10-07 · v0.2.0rc37" width="179" height="20"></picture></strong>：把真实经历变成可核对的资料：人工审核引用具体结果版本，确认、拒绝和证据不足分别保存；结果纠错会使旧审核过时，晚到审核不提前进入历史回放。合成样例单列，事件真实性、训练和误差仍需真实验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/f0ebfa8d4c398897-dark.svg"><img src="assets/readme-labels/f0ebfa8d4c398897-light.svg" alt="2026-10-07 · v0.2.0rc36" width="179" height="20"></picture></strong>：看清长采集实际留下什么：逐店核对完整记录链、北京时间日期与小时、请求跨度和成功请求缺口；较早预算结束留下的尾部缺口不会隐藏，重启和未知结果不拼成连续叫号。报告只读、零新增查询，真实全天质量继续验证。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/2c20dcb328319afa-dark.svg"><img src="assets/readme-labels/2c20dcb328319afa-light.svg" alt="2026-10-07 · v0.2.0rc35" width="179" height="20"></picture></strong>：把云端网络验证独立出来：手动触发固定三家试点的一次匿名查询，首错停止、无自动重试，只公开聚合状态。普通软件检查与真实网络结果分别记录，继续完成长时质量与实际部署。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/c5f514359f74aed1-dark.svg"><img src="assets/readme-labels/c5f514359f74aed1-light.svg" alt="2026-10-06 · v0.2.0rc34" width="179" height="20"></picture></strong>：把采集搬进Linux容器：非root运行、私有状态保存、宿主本机号码展示和安全恢复；重建服务沿原期限与预算，不把旧数据当作正在刷新。真实上游查询和合成重建分别验证，实际云端继续验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/35d91619fc8795c8-dark.svg"><img src="assets/readme-labels/35d91619fc8795c8-light.svg" alt="2026-10-06 · v0.2.0rc33" width="179" height="20"></picture></strong>：让历史回放更诚实：新匿名观测单独保存程序接收时间，补录不会被提前当成已知数据；旧行缺失、损坏、迟到和时间倒退分别处理。20项专项与920项完整检查通过，真实预测仍需结果与误差验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/61fe5e6e4024aa9c-dark.svg"><img src="assets/readme-labels/61fe5e6e4024aa9c-light.svg" alt="2026-10-06 · v0.2.0rc32" width="179" height="20"></picture></strong>：看清号码变化，也看清数据缺口：匿名来源新增日期与短时窗口分析，队列/数量各按自己的响应时间比较，失败与重启处断链，整份观测未接收完不提前使用；资料不足、陈旧和零基线明确显示。22项专项与900项完整检查通过，独立安装和三份真实记录/三个时点回放通过，原库不变、联网0；真实过号率、预测和提醒继续验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/1702e2aa6e29994c-dark.svg"><img src="assets/readme-labels/1702e2aa6e29994c-light.svg" alt="2026-10-06 · v0.2.0rc31" width="179" height="20"></picture></strong>：让共享刷新经得起重启：普通时段持续背景采集，临近用餐按更紧迫的计划切换60／30秒；保存原始截止时间、总预算和结果链，已保存结果不重查、未知尝试不补发，计划或数据库变化停止。28项专项与878项完整检查通过；隔离安装和真实恢复通过，六次请求全部成功，原期限/预算保留，五次读取增加上游请求0；全天/部署继续验证。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/d0f98200ac63cf0a-dark.svg"><img src="assets/readme-labels/d0f98200ac63cf0a-light.svg" alt="2026-10-06 · v0.2.0rc30" width="179" height="20"></picture></strong>：后台采集和查看号码可以同时进行：唯一工作者保存数据后发布只读展示状态，页面读取不触发重复查询；五类数组、未知单位数量、最近失败和历史信息分别保留。支持原任务恢复与正常退出，完成或停止后明确保留为旧信息；22项专项与850项完整检查通过；隔离安装及真实HTTP联动通过，三对六次请求全部成功，五次读取增加上游请求0。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/81a75ee321635880-dark.svg"><img src="assets/readme-labels/81a75ee321635880-light.svg" alt="2026-10-06 · v0.2.0rc29" width="179" height="20"></picture></strong>：让已经取得的观测经得起中断：保存固定任务进度，恢复时核对全部已提交结果，已完成槽位不重查、未知槽位不补造或重发；保持原预算与完整等待，失败和完成任务再次恢复均不查询。22项专项与828项完整检查通过；独立安装与真实中断恢复通过，三对六次请求全部成功；全天/服务器和预测继续推进。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/554442d13020971d-dark.svg"><img src="assets/readme-labels/554442d13020971d-light.svg" alt="2026-10-06 · v0.2.0rc28" width="179" height="20"></picture></strong>：让匿名采集跟随用餐计划调整节奏：合并同店关注需求，提前重算30／15分钟窗口，按60／30秒目标查询；每份成对查询最多两个请求，明确时长与预算，首错停止，遗漏时段只查当前一次。13项专项模拟与806项完整检查通过；预测、提醒及匿名真实调度另行验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/605bf8bc4d75ea54-dark.svg"><img src="assets/readme-labels/605bf8bc4d75ea54-light.svg" alt="2026-10-06 · v0.2.0rc27" width="179" height="20"></picture></strong>：找到无需微信凭证的独立排队查询路线，并实现单次采集、有界批量采样和只读报告。完整保留五类展示数组，将原始数量与等待分钟分开，两次查询的时间分别记录；失败停止、不密集重试。25项新增、793项完整检查及初安装通过；三店30秒/30轮实测180次请求全部成功，实际间隔约29.79–30.21秒；源延迟、长期与服务器验收继续推进。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/329e2453b2c0fe7e-dark.svg"><img src="assets/readme-labels/329e2453b2c0fe7e-light.svg" alt="2026-10-06 · v0.2.0rc26" width="179" height="20"></picture></strong>：新增独立结果首次接收库与只读回放，按本机实际接收顺序选择修订，防止补录的旧经历提前进入历史资料；保留不确定提交状态和私人输出，尚不认证真实性或训练资格。20 项新增、66 项专项及 768 项完整检查通过；午间实际更新缺口约 143 秒，继续改进自动恢复。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/9ed8d9831bac2761-dark.svg"><img src="assets/readme-labels/9ed8d9831bac2761-light.svg" alt="2026-10-06 · v0.2.0rc25" width="179" height="20"></picture></strong>：门店展示增加同库私有任务关联，多店同步显示保存的停采状态；任务过时或停止后号码保留为最后已知，拒绝跨库关联和未来状态，后台存活仍明确未知。13 项新增、66 项专项与 748 项完整检查通过，继续验证安装与实际长采集。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/d5120f5fb79a8573-dark.svg"><img src="assets/readme-labels/d5120f5fb79a8573-light.svg" alt="2026-10-06 · v0.2.0rc24" width="179" height="20"></picture></strong>：为普通与持久采集增加可选的有限 502、503、504 容忍额度，失败照常保存，后续查询守住时间间隔；重启不重置额度，401 与非法上下文仍停止。12 项新增、62 项专项及 735 项完整检查通过；当前午间实测仍用上一安装版，未人为制造上游错误。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/646fc283e14e430d-dark.svg"><img src="assets/readme-labels/646fc283e14e430d-light.svg" alt="2026-10-06 · v0.2.0rc23" width="179" height="20"></picture></strong>：新增门店展示出口，同看堂食和预约有限号码、响应年龄与刷新结果；数据过时、采集停检和查询失败明确保留旧信息。19 项专项及 723 项完整检查通过；上一恢复版营业三店 30 秒采集完成 90 次成功，正常更新缺口约 102 秒。用户页面、预测和长期自动更新继续验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/38f3874d8754bd25-dark.svg"><img src="assets/readme-labels/38f3874d8754bd25-light.svg" alt="2026-10-06 · v0.2.0rc22" width="179" height="20"></picture></strong>：减少多店恢复后的额外等待，同时保留上一轮完成后的间隔保护；提前更新、较晚更新、慢响应和显式重启分别验证，50 项专项与 704 项完整检查通过。补齐西单同次响应的签到桌数对照，保留源新鲜度未知状态。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/36b3d307de4822c3-dark.svg"><img src="assets/readme-labels/36b3d307de4822c3-light.svg" alt="2026-10-06 · v0.2.0rc21" width="179" height="20"></picture></strong>：新增本机投递、签名回执核对与私有确认保存/离线重开；19 项新增检查与 697 项完整检查通过。丢失回执后的去重重试和慢响应截止已验证，原包保留，继续实现远程部署与长期采集。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/971ab87ab723c44b-dark.svg"><img src="assets/readme-labels/971ab87ab723c44b-light.svg" alt="2026-10-06 · v0.2.0rc20" width="179" height="20"></picture></strong>：新增有界本机接收服务，独立接收凭证、完整校验、事务归档及对应确认；24 项新增检查与 678 项完整本机检查通过。慢连接截止和已提交丢响应后的同包重试已验证，远程部署与持久确认仍在实现。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/1bda4b5e8096c31f-dark.svg"><img src="assets/readme-labels/1bda4b5e8096c31f-light.svg" alt="2026-10-06 · v0.2.0rc19" width="179" height="20"></picture></strong>：新增本机持久待确认目录，容量有界、相同包不重复保存、异常保留旧资料。提供只读状态核对，14 项新增检查及 654 项完整本机检查通过；上传与可信确认仍在实现。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/9b6a8e051ea2ef6c-dark.svg"><img src="assets/readme-labels/9b6a8e051ea2ef6c-light.svg" alt="2026-10-06 · v0.2.0rc18" width="179" height="20"></picture></strong>：新增公共字段包格式与内容校验、本机事务归档；重复资料只保留一份，内容冲突保留原记录并回滚整批。提交异常区分未提交、已提交和结果未知，25 项新增检查及 640 项完整本机检查通过。上传、远程接收器与持久确认队列继续实现。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/2fa26719104da7b2-dark.svg"><img src="assets/readme-labels/2fa26719104da7b2-light.svg" alt="2026-10-06 · v0.2.0rc17" width="179" height="20"></picture></strong>：新增本机门店公共字段分页导出，保留缺失信息、号码顺序与失败时间语义；稳定观测标识和内容校验为后续重复数据检查准备格式。40 项新增检查、615 项完整本机检查通过，源库只读、拒绝覆盖及提交前后故障状态已验证；上传、待确认队列与服务器继续实现。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/b62e66a254a0dea2-dark.svg"><img src="assets/readme-labels/b62e66a254a0dea2-light.svg" alt="2026-10-06 · v0.2.0rc16" width="179" height="20"></picture></strong>：单店在正常凭证更新成功后保留原采样目标，错过目标只查询一次当前状态；下一次从新周期开始，减少额外等待。多店和跨进程重启保持完整周期，43 项专项与 575 项完整本机检查通过；最终包本轮真实跨更新间隔 98.760 秒，恢复后约 30 秒，长期稳定性继续验证。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/1b7386d87f5a032f-dark.svg"><img src="assets/readme-labels/1b7386d87f5a032f-light.svg" alt="2026-10-06 · v0.2.0rc15" width="179" height="20"></picture></strong>：同店共享策略接入有界采集，按临近用餐的 60／30 秒目标运行；实际开始锚定、窗口边界重算、查询预算与首错停止，29 项专项和 570 项完整本机检查通过。新凭证退出小程序后三店再次成功；另一次 504 后保留失败槽位，显式完成剩余四份。安装包在小程序关闭后按约 60／30 秒完成四次真实查询；闭店数据不用于验证营业叫号。长期调度、提醒与预测继续验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/f95864f3c813c2da-dark.svg"><img src="assets/readme-labels/f95864f3c813c2da-light.svg" alt="2026-10-06 · v0.2.0rc14" width="179" height="20"></picture></strong>：新增结果声明的历史回放，检查完整修订链，保留早先版本，分开统计未审核叫号候选、取消、过号和未观察完成。24 项专项与 541 项完整本机检查通过；真实接收证据、训练资格与模型回测继续推进。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/7e91bbcce0625617-dark.svg"><img src="assets/readme-labels/7e91bbcce0625617-light.svg" alt="2026-10-06 · v0.2.0rc13" width="179" height="20"></picture></strong>：新增私有区间误差计算，保留叫号时间的不确定范围，区分确认覆盖与可能覆盖；空资料明确显示缺失。32 项专项及 517 项完整本机检查通过，真实结果审核、模型回测与预测继续推进。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/ecfc0b982f5dc444-dark.svg"><img src="assets/readme-labels/ecfc0b982f5dc444-light.svg" alt="2026-10-06 · v0.2.0rc12" width="179" height="20"></picture></strong>：新增短窗协调工具，独立守护进程保持限时关闭，接收结束后提前通知关闭，并保留主配置与私有暂存。29 项专项、485 项完整本机检查及 GitHub 三版本安装检查通过；正常新凭证接入与原进程恢复已实测，到期前仍沿用旧凭证的情况被正确拒绝。长期自动运行继续验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/dda7e700869c57f1-dark.svg"><img src="assets/readme-labels/dda7e700869c57f1-light.svg" alt="2026-10-06 · v0.2.0rc11" width="179" height="20"></picture></strong>：新增同店共享监控策略，多个关注计划共用一份查询目标，取更紧迫的刷新节奏，并在 30／15 分钟窗口边界及时重算；断采后不补发积压请求。18 项专项及 456 项完整本机检查通过，当前仅离线规划，持续调度和提醒仍待实现。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/ce7fbc78085b759a-dark.svg"><img src="assets/readme-labels/ce7fbc78085b759a-light.svg" alt="2026-10-06 · v0.2.0rc10" width="179" height="20"></picture></strong>：新增离线用餐时间策略，正确处理 `+10`／`-10`、30／15 分钟边界、提前叫号估计和加速输入。17 项专项及 438 项完整本机检查通过；后台调度、真实预测和提醒继续分阶段实现。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/a5653c4e30bc4dd0-dark.svg"><img src="assets/readme-labels/a5653c4e30bc4dd0-light.svg" alt="2026-10-06 · v0.2.0rc9" width="171" height="20"></picture></strong>：新增调试关闭后的完整暂存提交，检查新授权、同应用和版本，在协作锁内核对旧配置并原子保存。26 项专项及 421 项完整本机检查通过；实际接入与长期自动更新继续分别验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/e728f12e267c9e8d-dark.svg"><img src="assets/readme-labels/e728f12e267c9e8d-light.svg" alt="2026-10-06 · v0.2.0rc8" width="171" height="20"></picture></strong>：新增独立短窗关闭保护，固定命令、状态检查、超时回收和异常清理；23 项专项及 395 项完整本机检查通过。已验证保持关闭与到时回读，完整自动更新继续推进。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/7610b7cb350cfbcb-dark.svg"><img src="assets/readme-labels/7610b7cb350cfbcb-light.svg" alt="2026-10-06 · v0.2.0rc7" width="171" height="20"></picture></strong>：新增私有有界任务进度、显式重启恢复和离线任务状态；已提交快照按同次尝试对账，未知缺口单列，跨重启拒绝旧版本或冲突凭证。27 项专项和 372 项完整检查通过，西单真实中断后续采 1+2 份成功快照；持续推进正常更新控制器。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/c6802dfe9e84937c-dark.svg"><img src="assets/readme-labels/c6802dfe9e84937c-light.svg" alt="2026-10-06 · v0.2.0rc6" width="171" height="20"></picture></strong>：新增有界本机正常目录摘要接入，整组新上下文私有保存；按真实摘要修正记录数与完成时间格式，21 项专项与 345 项完整本机检查通过。正常目录新凭证、三店独立查询及一次无需重启的跨到期恢复已实测，继续长期可靠性验证。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/cf34e6ba46337826-dark.svg"><img src="assets/readme-labels/cf34e6ba46337826-light.svg" alt="2026-10-05 · v0.2.0rc5" width="171" height="20"></picture></strong>：增加可选接收诊断，只报告连接与投递计数和固定拒绝原因，保护凭证内容；324项完整本机检查通过。记录电脑正常刷新的一次真实接入及后续未收到有效投递的结果，继续验证稳定更新与独立查询。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/cc659ae1fac450d5-dark.svg"><img src="assets/readme-labels/cc659ae1fac450d5-light.svg" alt="2026-10-04 · v0.2.0rc4" width="171" height="20"></picture></strong>：增加只读展示集合窗口分析，保留相邻观测覆盖、前后速率比较、未来数据排除和失败断链；19项专项及317项完整本机检查通过。已回放三店历史窗口，真实过号率、告警和预测仍待验证。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/b7611366fa364d92-dark.svg"><img src="assets/readme-labels/b7611366fa364d92-light.svg" alt="2026-10-04 · v0.2.0rc3" width="171" height="20"></picture></strong>：增加 2026 年完整日期分类、时区换算与历史公告可用检查；包内保存核对版本/来源，未知年份不猜测。298 项本机检查通过；这些特征尚未接入预测模型。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/a0c29b36cf31c3fc-dark.svg"><img src="assets/readme-labels/a0c29b36cf31c3fc-light.svg" alt="2026-10-04 · v0.2.0rc2" width="171" height="20"></picture></strong>：增加本机结果校验、独立私有修订追加库和只读统计；保留事件时间区间，区分叫号、过号、取消与未完成。283 项本机检查通过，结果真实性/训练资格、预测与真实续期仍待验证。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/f58682a4b846e4e5-dark.svg"><img src="assets/readme-labels/f58682a4b846e4e5-light.svg" alt="2026-10-04 · v0.2.0rc1" width="171" height="20"></picture></strong>：完成三店 30 秒各 67 轮真实采集及到期停采验证；补齐字段/时间语义、缓存和配额观察、私有默认目录、261 项检查与安装/CI 流程。保留真实恢复未通过的结果，正式 v0.2.0 继续验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/988e2b76ec0832d6-dark.svg"><img src="assets/readme-labels/988e2b76ec0832d6-light.svg" alt="2026-10-04 · README" width="147" height="20"></picture></strong>：用餐体验区采用无框像素刷痕设计，与 Logo 配色呼应；适配浅色、深色和手机阅读，详细规则可以展开查看。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/90668b61e5617f25-dark.svg"><img src="assets/readme-labels/90668b61e5617f25-light.svg" alt="2026-10-04 · v0.2.0.dev5" width="187" height="20"></picture></strong>：实现同电脑短时上下文接收、配套响应适配器及采集到期后的可选等待恢复；252项检查通过。补充正常登录与签名来源证据；真实辅助端联动、连续跨到期与服务器独立更新仍待验收。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/0229aa29b91e817f-dark.svg"><img src="assets/readme-labels/0229aa29b91e817f-light.svg" alt="2026-10-03 · v0.2.0.dev4" width="187" height="20"></picture></strong>：新增私有凭证文件的零网络状态检查，与实际采集共用到期保护规则；226项检查通过。整理正常更新证据与参数缺口，自动续期仍待实现。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/508d4160332f665e-dark.svg"><img src="assets/readme-labels/508d4160332f665e-light.svg" alt="2026-10-03 · Logo" width="131" height="20"></picture></strong>：采用 Unbounded 浅／深色矢量 Logo，随 GitHub 主题自动切换；保留红色像素刷痕、边缘渐隐和柔和黑白文字渐变；同步字体许可及来源说明。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/742faee7250e5675-dark.svg"><img src="assets/readme-labels/742faee7250e5675-light.svg" alt="2026-10-03 · v0.2.0.dev3" width="187" height="20"></picture></strong>：新增离线检查、显式私有导入及目录到期保护，212 项检查通过；电脑目录查询和三店详情成功，60 秒／30 秒短窗采样各两轮完成，累计保存 18 份详情快照；确认正常初始化的新凭证来源，显式更新后再次查询三店成功。独立自动更新是下一步重点。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/782a408982eaed7a-dark.svg"><img src="assets/readme-labels/782a408982eaed7a-light.svg" alt="2026-10-03 · v0.2.0.dev2" width="187" height="20"></picture></strong>：新增私有查询上下文的运行中整组更新、声明到期前保护停采，以及请求间隔、错误分类和公共字段变化报告；166 项离线检查通过。标题改用开放许可的 Montserrat Light 轮廓 SVG，适配浅色与深色模式。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/4486edd74871b8a8-dark.svg"><img src="assets/readme-labels/4486edd74871b8a8-light.svg" alt="2026-10-02 · v0.2.0.dev1" width="187" height="20"></picture></strong>：发现并适配官方小程序实际查询地址，核对西单店 ID 与展示队列；新增来源隔离和本机凭证状态检查；观察到凭证声明到期后的 401，将正常授权续期列为下一步重点；补充同步叫号页面目标并改进产品介绍。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/326f209ecd758a90-dark.svg"><img src="assets/readme-labels/326f209ecd758a90-light.svg" alt="2026-10-02 · v0.2.0.dev0" width="187" height="20"></picture></strong>：实现只读验证工具、离线回放和本地存储；完成匿名接口连通测试，带有效查询鉴权的真实采集待验证。

- <strong><picture><source media="(prefers-color-scheme: dark)" srcset="assets/readme-labels/1b1466241b7e034f-dark.svg"><img src="assets/readme-labels/1b1466241b7e034f-light.svg" alt="2026-10-02 · v0.1.0" width="147" height="20"></picture></strong>：建立项目；整理完整需求、技术研究、阶段路线和持续交接规则。

每次能力更新都会同步改进本页的功能介绍与阶段状态。完整项目背景、设计要求、版本迭代与每次编辑记录见 [docs/PROJECT_HANDOVER.md](docs/PROJECT_HANDOVER.md)；参考项目分析见 [docs/REFERENCE_REVIEW.md](docs/REFERENCE_REVIEW.md)。
