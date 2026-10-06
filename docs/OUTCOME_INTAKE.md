# 结果首次接收与修订回放

rc37补充[人工审核声明](OUTCOME_REVIEWS.md)：审核绑定具体首次接收版本，保留纠错/冲突及审核接收时刻；它不修改本账本，也不自动认证真实标签。

rc26增加 `outcome-receive` 和 `outcome-received-cohort`。输入仍是[本人结果记录](OUTCOMES.md)的未审核事件与时间区间；程序另行记录首次接收时间，回放时按实际本机接收顺序选择版本。它补充旧[声明时间回放](OUTCOME_COHORT.md)，为后续资料审核准备证据，当前没有真实训练标签、预测特征或 ETA。

## 私有使用方式

先准备当前用户拥有的0700目录，输入文件和数据库均须0600、单硬链接、路径无符号链接。正式输入来自本人已有经历，不为采集制造取号或取消操作。

```sh
python -m sushiwait outcome-receive \
  --input /absolute/private/episode.json \
  --db /absolute/private/intake.sqlite3

python -m sushiwait outcome-received-cohort \
  --db /absolute/private/intake.sqlite3 \
  --as-of 2026-10-06T12:30:00+08:00 \
  --data-origin self_reported --api-profile miniapp_gateway
```

示例截止时刻仅表示格式，运行时不得晚于本机当前时间。合成软件检查可将输入替换为 `--synthetic-fixture examples/fixtures/outcome-01.synthetic.json`，回放来源改为 `synthetic`；真实与合成资料分别统计。

数据库采用独立intake schema1，不能直接打开或自动迁移旧结果schema1、公共快照schema2、归档库。旧库不能凭调用者声明补出曾经真实发生的接收时间。首次写入可以建立空库；回放只读既有库。没有自动发现文件、查询门店、读取凭证或操作客户端。

## 接收顺序与幂等

`recorded_at`仍是调用者声称的记录时刻。`received_at`由程序在写事务内用本机UTC时钟产生，输入不得提供或覆盖。数据库同时保存规范化原记录与绑定接收时间的内容校验值；同一事件修订从1连续递增，门店、来源、接口和队列不变，声明时间不倒退。

重复提交相同事件和版本、且内容相同，会保留首次接收时间和文件内容；即使更高修订已存在，也不重写旧版本。同版本不同内容、跳跃版本、身份冲突或本机接收时间倒退会停止。每条输入限16KiB，整个库最多10,000条修订，不能通过无限写入绕过审计上限。

提交前失败说明未开始提交；提交调用失败可能结果未知，不自动重发；提交成功但后续持久化确认失败明确报告已提交，不能称未写入。调用者应对原库进行核对，再决定是否重复相同输入。

## 截止时刻选择

只读回放在一致事务中完整核对有界修订链、列与内容、校验值、全局接收顺序及权限，再对每个事件选择 `received_at ≤ as_of` 的最高修订。相同接收时刻取更高版本；截止之后的纠错不会覆盖当时已接收的旧版本。

例如一份声称11:00记录的经历，实际12:00才提交：11:30回放不会纳入它。12:10收到纠错，则12:05保留原版，12:10使用纠错版。这个区别防止用调用者回填的历史时间制造过去已经掌握的信息。

可用 `--max-revisions` 设置1–10,000的完整审计上限。截止后的版本和其他来源仍参与完整性检查；超限或任意坏记录整批拒绝，不把最新片段冒充完整历史。因此扫描数等诊断不能作为当时预测特征。只统计终止状态、未审核叫号/入座候选与右删失数量，不返回个人标识、行程或具体等待时长。

## 证据边界

这是合作使用程序条件下的本机首次接收账本，不是外部可信时间戳、不可篡改存证或事件真实性审核。拥有本机和数据库的用户可以改时钟或重算校验值；校验值只能发现未同步重算的损坏，不能认证提交者。接收时钟在插入和提交完成之前读取，也不能证明某一历史时刻已持久落盘。

输出固定保留 `availability_basis=local_first_receipt_time`、`receipt_revision_chains_checked=true`，并明确 `independent_time_attestation=false`、`historical_availability_verified=false`、`durable_availability_verified=false`、`authenticity_verified=false`、`verified_training_labels=0`、`training_eligible=false`、`prediction_features_exported=false`、`eta_available=false`。私人聚合也不能自动公开或传给LLM。

下一步需要真实现场结果、独立审核记录、预测日志与数据版本，按时间和事件划分训练及验证。20项新增检查覆盖回填历史、纠错截止、重复接收、冲突、接收时钟倒退、完整链、私有权限、提交不确定性与CLI零外部调用。完整套件、最终安装、公开和真实试验分别见[交接记录](PROJECT_HANDOVER.md)。
