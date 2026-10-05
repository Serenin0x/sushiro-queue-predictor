# 历史结果声明与完整修订链

rc14新增 `outcome-cohort`，只读[本人结果库](OUTCOMES.md)，在一个一致数据库视图内检查完整有界修订链，再按记录声称的时刻选择当时版本。用途是准备结果审核、发现资料缺口与检查历史回放；没有导出预测特征、训练模型或认证真实标签。完整预测契约见[DATASET_DESIGN](DATASET_DESIGN.md)与[预测评估方案](PREDICTION_EVALUATION.md)。

## 私有运行入口

```sh
python -m sushiwait outcome-cohort --db /absolute/private/outcomes.sqlite3 \
  --as-of 2026-10-06T07:00:00+08:00 \
  --data-origin self_reported --api-profile miniapp_gateway
```

明确指定结果库、带时区和秒的历史时刻、来源和接口版本；示例时刻只用于说明语法，实际运行时不能晚于本机当前时刻。结果库必须已存在，属于当前用户、0600、单硬链接，父目录0700，路径无符号链接；沿用结果库的私有读取和身份核对。不会自动建库、搜索用户资料、读取公共快照/凭证、查询门店或操作微信/Surge。

一次最多核对整个库的10,000条修订，可用 `--max-revisions` 设为1–10,000。限制针对完整修订数，而非最新episode数；超过限额整体拒绝，不截取最新记录假装完整历史。每条payload另限16KiB，超限/损坏/列与内容不符同样整批失败，仅输出固定错误码。

## 历史选择与修订核对

先读取全部有界修订并校验结果格式；同一episode的版本必须从1连续递增、无重复，记录时刻不倒退，门店/profile/来源/队列不混换。之后在每个episode中选择 `recorded_at ≤ as_of` 的最高版本，并只统计指定来源/profile。相同时刻的纠错选更高版本；不能因最新修订在截止时间之后，就丢掉仍有效的旧版本。

例如原版本在11:00记录叫号结果，12:00修正为取消：重建11:30保留原版本，重建12:00使用修正。所有修订保留，取消/过号/观察结束不会补成叫号；没有叫号且观察结束单列右删失。只有已记录的叫号/入座分别计为未审核候选，不把精确时间格式当作真实精确事件。

数据库通过只读连接与显式读取事务保持一份视图，结束释放自身事务，不接管调用方已有事务。完整库中其他来源及截止后的修订仍参与资料完整性核对；任何坏修订使本次检查失败。因此它是资料审计，未来修订的扫描数量等诊断不能当作当时预测特征。

## 输出与真实性边界

输出仅修订数、选中episode数、终止状态分布、未审核叫号/入座候选及删失数，不返回个人标识、门店行程、事件时刻或等待时长。聚合也是私人资料，小样本不能自动视为匿名并公开或传给LLM。

当前结果库schema1没有不可变的实际接收/提交时间，`recorded_at`来自调用者声明。代码不能证明一条声称11:00的记录真的在11:00前已存入本机，也不能保证调用者提交了完整现实总体。因此固定输出 `availability_basis=caller_recorded_at_claim`、`historical_availability_verified=false`、`authenticity_verified=false`、`verified_training_labels=0`、`training_eligible=false`、`prediction_features_exported=false`、`eta_available=false`。

`claim_revision_chains_checked=true`只说明所读声明的修订结构通过，不是事实审核。后续真实回测需独立接收证据、审核记录、资料版本、预测日志以及按时间/episode划分；不能从本报告自动提升级别。现有结果库schema1和公共快照DB2保持。

24项专项覆盖截止前后纠错、等时版本、来源隔离、完整链/坏修订拒绝、删失分类、私有库权限及CLI零外部调用；完整套件、安装和实际公开结果以最新[交接记录](PROJECT_HANDOVER.md)为准。合成样例只检验软件，不构成真实排队结果或模型成绩。
