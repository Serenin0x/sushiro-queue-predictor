# 多日连续采集计划

候选rc42新增`remote-campaign-collect`、`remote-campaign-serve`和只读`remote-campaign-status`。它把最多三店、最多14天的明确采集计划分段保存：正常窗口结束后自动接续，总截止与成对查询预算从最初创建起固定，不随跨日、重启或计划修改增加。默认7天、每段24小时、300秒背景周期、6300对/最多12600次GET；这是运行配置，不是已完成七天实测。

## 本机入口

使用专用空0700目录；不可变计划和可选的版本化更新文件放在该目录之外。新计划不会采用已有窗口、旧数据库或丢失检查点后的文件。实际查询仍是匿名CRM来源，无需上传微信凭证。

```sh
mkdir -m 700 /private/local/sushiwait-week
sushiwait remote-campaign-collect \
  --root /private/local/sushiwait-week \
  --plan-file /private/local/plans.json \
  --store-id 3014 --store-id 3004 --store-id 2009 \
  --base-interval 300 --duration 604800 --window-duration 86400 \
  --max-pairs 6300 --resume-if-present
sushiwait remote-campaign-status --root /private/local/sushiwait-week
```

路径是操作者已有私有目录下的示例，`plans.json`使用0600和`{"schema_version":1,"plans":[]}`。显式`--plan-updates-file`沿用[版本化计划协议](MONITORING_PLAN_UPDATES.md)；同店需求共享60/30秒周期，系列、修订和声明回退拒绝。正常跨窗口继续保留已接受的最低修订，归档核验不依赖之后修改的计划文件。

`--resume-task`只恢复已有计划；`--resume-if-present`只在专用目录没有既有资料时新建。状态读取只打开检查点，不打开数据库、不查询门店，进程存活标为未知。原`remote-window-*`命令、72小时窗口上限和原任务契约保持；本入口不迁移已经运行的窗口。

## 持续运行和停止规则

- 每段保存独立`window-01/remote.sqlite3`及`task.json`，顶层`campaign.json`保持总期限、总预算、已保存/未知槽位和归档摘要；目录0700、文件0600。正常完成后先关闭写者，再核对文件身份、状态与SHA256，旧段保留。
- 同店查询开始时间跨窗口保留墙钟与当前进程单调间隔。窗口边界不额外查询，重启等待当前完整周期，缺少的过去不补抓。重启等待属于整份计划，短窗口到期不能重置或消除它。
- HTTP/解析失败沿用首错停止，整份计划停止；没有保存结果的中断请求记未知并消耗一对预算，接续前停止。已经保存而尚未对账的结果按原任务核一次，不重发。
- 窗口提前用完预算或单调时钟先到而总体期限尚未到时停止，不能靠换窗口获得新额度或产生重叠。末段不足30秒时调整相邻分界，每段仍不超过72小时，总期限不变；不足一整秒的末尾等待原截止。
- 计划到期、总预算用完或失败后，重启只展示已保存历史，健康503、不创建新客户端或下一份计划。新一轮须明确使用另一个专用状态目录和预算，保留本轮资料。
- 单份计划独占锁；原库/任务丢失、身份变化、损坏、孤立未发布窗口或配置冲突均停止并保留现场。本版没有自动修复孤立目录、跨主机迁移、自动失败重试、滚动删库或永久守护。

运行中展示继续由数据库所属工作者发布副本；HTTP读取不会另查寿司郎。归档数据库只在无所属写者时按只读模式检查，外部不能为展示打开当前运行库。

## Linux新项目

基础Compose加本次配置创建独立新项目，不复用既有全天测试或schema2/3卷。使用本机私有计划管理入口，先初始化计划卷，再启动：

```sh
docker compose -p sushiwait-week -f deploy/compose.yaml -f deploy/compose.campaign.yaml build
docker compose -p sushiwait-week -f deploy/compose.yaml -f deploy/compose.campaign.yaml \
  run --rm --no-deps --entrypoint python collector \
  -I /opt/sushiwait/plan_admin.py init --store-id 3014 --store-id 3004 --store-id 2009
docker compose -p sushiwait-week -f deploy/compose.yaml -f deploy/compose.campaign.yaml up -d
curl --fail http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/api/v1/status
docker compose -p sushiwait-week -f deploy/compose.yaml -f deploy/compose.campaign.yaml \
  exec collector sushiwait remote-campaign-status --root /state/campaign
```

独立项目仍使用宿主127.0.0.1:8765；同时运行其他服务时需明确改宿主端口，不能冲掉旧采集。计划发布/清空/status沿用[Linux计划管理](LINUX_PLAN_UPDATES.md)，以服务状态中的`task.accepted_plan_revision`确认已接受。Compose没有自动失败重启；正常窗口接续由同一工作者负责。

## 验证范围

30项新增检查使用真实私有文件、合成上游与合成时钟，覆盖跨窗口间隔、总体期限/预算、重启、未知/已保存中断、失败及归档后崩溃、文件身份、来源边界、运行中计划和只读ASGI。原窗口和计划专项合计87项通过；完整1117项14.346秒通过。合成结果不构成真实多日覆盖或可靠预测成绩。

`scripts/check_container_campaign.py`已在最终镜像实际运行144.899秒：3个Linux容器、3段归档、合成4对8GET/官方0，实际间隔65.879/30.010/30.010秒。运行中接受revision1–3，终态文件revision4不复活任务；重建后DB/检查点/种子不变，展示5次读取增加来源请求0，健康503。静态63.842秒/恢复60.012秒和动态计划125.120秒/60.016、30.008秒是独立检查，不混计。全部合成上游，真实墙钟/单调时钟和容器生命周期，不能称真实门店或生产主机验收。

最终44模块wheel/checkout外`-I`安装已验，多日路径合成3段/3对6GET、socket0/凭证0，终态文件不变。另用该安装版对西单做185秒有限真实计划，70秒目标分段/60秒背景/最多5对10GET：自然结束实际185.017秒，3段全部归档、4对8GET全200，实际60.011/60.013/60.001秒，原计划不变，终态恢复额外请求0/凭证0/文件不变。没有操作微信、读取登录凭证、取号或取消；当时官方小程序仍开着，不能将此轮写成“小程序退出后”的新实测。详见[交接记录E0278](PROJECT_HANDOVER.md)，这只是短时真实自动接续，七天和逐店覆盖仍待长期任务验证。

逐店真实日期覆盖、首尾/内部缺口、源更新时效和数量单位仍单独验收，历史窗口可用[终态质量报告](ANONYMOUS_WINDOW_QUALITY.md)分析。归档哈希不是叫号/弃号标签，跨窗口汇总的成功数也不是连续质量认证。本入口不估时、不发提醒、不取号/取消、不提供微信前端或生产服务器地址。
