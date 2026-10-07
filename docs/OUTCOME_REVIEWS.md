# 人工审核声明与版本绑定

rc37补充[结果首次接收](OUTCOME_INTAKE.md)：为本人已有排队经历生成未批准的私有草稿，接收确认、拒绝或证据不足的人工结论，按结果和审核首次接收时刻回放。原经历纠错会使旧审核过时；合成资料单列。当前没有实际确认结果、预测模型或训练资格认证。

人工确认是审核者声明，程序不能证明事件真实发生或认证审核者身份。它不识别截图、查询个人号单、自动提升训练资格。authenticity_verified=false、review_identity_authenticated=false、verified_training_labels=0、training_eligible=false与ETA不可用保持；完整预测与真实性验收见[预测方案](PREDICTION_EVALUATION.md)。

## 私有流程

先用outcome-receive接收本人已有、按真实时间区间填写的结果。源库与审核输出使用不同0700私有父目录，避免写入与源库只读锁冲突；文件0600、路径无符号链接/多硬链接。示例路径需换成自己的位置，不自动创建目录或寻找个人资料。

```sh
sushiwait outcome-review-draft \
  --source-db /private/outcomes/intake.sqlite3 \
  --input /private/outcomes/episode.json \
  --output /private/reviews/draft.json
```

输入须精确匹配源库最新修订。草稿绑定episode、修订和源首次接收校验值，生成随机审核及未认证审核者ID；默认insufficient_evidence/uncertain_time，三项核对均false。已有输出不会覆盖；发布后持久化确认失败明确committed=true、durability_confirmed=false及非零退出，先核对文件，不盲目重发。

审核者在本机亲自核对门店与堂食/预约、取号时间范围和叫号时间范围后，才可把decision改为accept、reason_code改为confirmed_call_interval，将issued_time_bounds_checked、called_time_bounds_checked、store_and_queue_checked设为true，reviewed_at填真实审核时刻。缺少called或队列unknown不能确认叫号等待区间；取消、过号或入座不能补成叫号。

证据不足保留insufficient_evidence；错误记录可reject。非确认原因限定missing_call、uncertain_time、mismatch或withdrawn，不保存任意备注、截图路径、号码、手机号或微信身份。真实记录evidence_kind为self_observation_confirmation，合成记录只能synthetic；合成不当现场确认。

```sh
sushiwait outcome-review-receive \
  --source-db /private/outcomes/intake.sqlite3 \
  --db /private/reviews/reviews.sqlite3 \
  --input /private/reviews/draft.json

sushiwait outcome-reviewed-cohort \
  --source-db /private/outcomes/intake.sqlite3 \
  --db /private/reviews/reviews.sqlite3 \
  --as-of 2026-10-07T12:00:00+08:00 \
  --data-origin self_reported --api-profile miniapp_gateway
```

截止带时区和秒，不能晚于当前时间；示例只表达格式，须选择真实过去时刻。审核接收时刻由程序在写事务内生成，输入不能提供。reviewed_at仍是声明，且不得早于所引用结果首次接收。新审核只允许引用当前最新结果。

## 纠错与冲突

同一审核ID从revision1连续递增，supersedes_revision为前版；审核者和episode不变，引用结果版本及声明审核时间不能倒退。纠错追加新修订，不覆盖旧结论。重复相同ID/修订/内容保留首次接收；同版本不同内容拒绝。提交错误区分未开始、结果未知和已提交，不自动重发。

回放分别选择截止前最新结果和审核。12:00才接收的审核不会使11:30已有的结果变成当时已确认；后来修改结果，即使只改人数，也使旧版审核过时，须重新核对。之后撤回确认，不覆盖此前截止的原结论。

同一结果有多个当前审核ID时，全部accept才计一个确认区间；任何reject优先拒绝，其余混合保持证据不足。冲突单列，不按未认证ID数量投票。人工声明确认数和合成确认数分开，不重复计算同一episode，不混来源/profile。

## 有界审计与输出

独立审核SQLite schema1，不迁移/改写首次接收库、旧结果库或公共快照库。完整审计两个库，各最多10000修订，max-revisions可设1–10000；其他来源和未来版本仍参与完整性核对。超限/坏行拒绝，不用尾部片段冒充完整链。SQL限制字段长度及每份payload16KiB；核对规范内容、接收校验值、修订链、全局顺序和精确源引用。

报告仅含决策、确认/拒绝/不足/冲突/过时数量，不输出ID、源校验值、事件时间、门店行程或等待区间。私人小样本聚合不得自动公开或传给LLM。两个库共享读保护保持配合程序的稳定读取，报告不改文件、联网或访问凭证。

availability_basis=local_source_and_review_first_receipts是本机时钟依据，不是外部可信时间或事件存证；historical_availability_verified=false、independent_time_attestation=false保持。确认声明为下一步证据审核和可追溯标签政策准备资料，不认证精度、过号率或等待时间。实际安装、检查、发布和现场结果分别记录在[交接历史](PROJECT_HANDOVER.md)。

rc44新增[私有经历与门店观测对齐](OUTCOME_FEATURES.md)，完整扫描已封存的有界匿名资料，按本机首次接收与整对完成时刻筛选预测输入；事后重建不是实际预测日志，真实性、训练资格和ETA均未认证。正在运行库仍拒绝外部读取。
