# 本机排队结果记录

0.2.0rc2新增离线结果校验、独立私有库追加导入和只读统计，为后续真实标签与评估准备资料。没有个人业务接口、自动取号、训练、预测或数据上传。正式v0.2接入条件继续见[V0_2_ACCEPTANCE.md](V0_2_ACCEPTANCE.md)；设计和证据口径见[DATASET_DESIGN.md](DATASET_DESIGN.md)。

## 能做什么

- outcome-check：验证本人明确指定的JSON结构、时间关系和来源，输出安全摘要；不建数据库。
- outcome-import：先完整验证，再追加至独立结果库。revision从1开始逐次递增；同一最新revision与相同规范化内容重复导入为幂等，不重复插入。旧revision和不同内容冲突不覆盖。
- outcome-report：只读统计最新episode修订，默认最多1000个、可选1–10000，标明窗口是否截断。合成样例与人工报告分别计数；无效最新记录仅计入invalid，不输出原文。

这三种命令不读取微信或查询凭证，不联网，也不验证人工事件是否确实发生。有效格式不是已经验收的训练标签。当前输出authenticity_verified=false、training_eligible=false或verified_training_labels=0；后续需实现证据审核和训练策略，不能把该工具的格式检查当成模型成绩。

## 输入与隐私

本人真实输入使用`--input`，POSIX常规文件、本人所有、0600或0400、单硬链接，父目录700或500；所有路径祖先不允许符号链接。有界读取最多16KiB，重复JSON键、非有限值、坏编码或读中变化停止。公开合成样例用`--synthetic-fixture`，同样最多16KiB且必须明确标synthetic；不借这个选项把本人资料伪装成可公开样例。

记录不含票号、手机号、微信ID、请求头、原始响应、截图或任意备注。未知字段会被拒绝。episode和event使用随机规范UUIDv4，不由个人标识生成。JSON和SQLite仍含精确事件时间及门店关联，只留本人安全本机目录，不放Git checkout、桌面同步目录、模型提示或公共仓库。

命令摘要/报告不输出episode/event ID、门店ID、具体行程时间或原始时间区间，仅返回结构、来源、修订和聚合状态。模块的规范化结果和候选区间属于私有数据，后续调用方也不能直接写日志或发送模型。

## schema 1 记录

完整合成样例见[examples/fixtures/outcome-01.synthetic.json](../examples/fixtures/outcome-01.synthetic.json)。它是虚构门店900001和2020年时刻，不能用于真实查询或模型成绩。

| 字段 | 规则 |
| --- | --- |
| schema_version | 整数1，布尔值不当整数 |
| episode_id | 随机、规范小写UUIDv4；一次排队服务尝试一个episode |
| revision / supersedes_revision | 1/null；其后n/n-1。格式校验不能证明历史存在，只有导入后才核对库中版本顺序 |
| store_id / api_profile | 正整数店号字符串及legacy或miniapp_gateway；来源和店号固定，修订不能跨店/profile |
| data_origin | self_reported或synthetic，不能写live冒称接口证据；修订不能换来源 |
| queue_type | ordinary、reservation或unknown；修订不能跨队列 |
| party_size / table_type | 未知人数保留null，否则正32位整数；桌型booth/counter/either/unknown。输入容量不代表门店取号资格 |
| recorded_at | 实际记录完成时间，显式时区；不能晚于本次检查时钟 |
| events | 1–7项，第一项issued；每种事件/ID最多一次，取消/过号/入座/结束最多一个且为末项 |

事件包含event_id、event_type、event_time_lower、event_time_upper、observed_at、evidence_kind及verification_status，缺一或多余均拒绝。event_type支持issued、checked_in、called、no_show、cancelled、seated、observation_ended；人工来源evidence_kind=self_observation，合成来源为synthetic，verification_status目前只能unverified。

下界不晚于上界、上界不晚于实际观察、观察不晚于记录；事件序列必须存在一种与时间区间一致的先后关系。区间允许重叠，不强行把不确定事件排成精确秒数。秒级时间戳必须有时区，统一为UTC字符串；六位小数仅为规范格式，不代表事件真的精确到微秒。时间只知道分钟或发生于两次观察之间时，应填真实区间。

本版本记录单次服务尝试，不覆盖返签、重新叫号和重复签到的全部业务规则；发生新的尝试用另一个episode，关联和业务规则留待后续验证。不为收集样本执行取号或取消。取消/未完成/过号没有called观察时，叫号目标保持缺失；observation_ended且没有called才标记“未观察到叫号的右删失”。seated没有called也可保留，但不能编造叫号时间。

模块为后续审核保留候选called等待/叫号到入座区间，按事件上下界作区间运算，重叠时下界最多截到零。这是对记录本身的算术，不是门店预测、真实过号率或已校准误差；候选目标不在公共命令输出中展示。

## 操作示例

先用本机私有目录，替换示例路径为自己的目录；不要把真实结果存入仓库。数据库父目录必须已经存在、本人所有且700，文件由工具新建为600；不自动修改既有目录或文件权限。仅macOS/Linux等POSIX系统支持私有导入和数据库；Windows未验收并明确拒绝这条私有路径。

```sh
SUSHIWAIT_OUTCOME_DIR="/absolute/path/to/your/private/outcomes"
mkdir -m 700 "$SUSHIWAIT_OUTCOME_DIR"
PYTHONPATH=src python3 -m sushiwait outcome-check --synthetic-fixture examples/fixtures/outcome-01.synthetic.json
PYTHONPATH=src python3 -m sushiwait outcome-import --synthetic-fixture examples/fixtures/outcome-01.synthetic.json --db "$SUSHIWAIT_OUTCOME_DIR/outcomes.sqlite3"
PYTHONPATH=src python3 -m sushiwait outcome-report --db "$SUSHIWAIT_OUTCOME_DIR/outcomes.sqlite3"
```

本人已有正常结果文件写好并设为600后，显式指定：

```sh
PYTHONPATH=src python3 -m sushiwait outcome-check --input "$SUSHIWAIT_OUTCOME_DIR/my-result.json"
PYTHONPATH=src python3 -m sushiwait outcome-import --input "$SUSHIWAIT_OUTCOME_DIR/my-result.json" --db "$SUSHIWAIT_OUTCOME_DIR/outcomes.sqlite3"
```

检验通过为exit0/ok=true，但不意味着真实性已验证。出错为固定安全代码且不输出原文；命令不自动重试。数据库是独立schema1，不能使用已有快照DB2；schema不符拒绝，不迁移或覆盖公共库。修订追加保留旧记录，报告按每个episode的最新revision统计；窗口较小时总episode/修订仍分别说明。

SQLite事务和目录锁序列化配合的本机写入者，写前/提交前重新检查文件和目录身份；冲突/检查失败回滚事件插入，已有修订不改。只读报告不创建数据库或迁移结构。常规模式用DELETE journal，库/唯一索引结构需符合预期；不支持未知旧表、触发器、WAL或随意改造的索引。拥有同一系统账号的不配合进程仍可能在文件系统最终检查后竞态，这不是防御同账号恶意软件的保证。

## 当前证据与下一步

本轮仅使用合成记录和模拟self_reported场景检查校验、区间、来源、修订、冲突/回滚、权限与摘要，不导入真实个人资料或污染219份公共快照主库。真实结果仍0份已验证训练标签；日历、特征导出、审核策略、训练和误差评估继续待实现。正常凭证供应和真实恢复仍是接入主线，前端进入条件保持。

rc13新增独立的[区间误差计算入口](INTERVAL_EVALUATION.md)，显式读取私有预测/叫号区间声明并输出私人聚合误差与覆盖界，不连接结果库或快照库、不审核标签、不认证预测日志/模型表现。完整资料审核、可训练标签及真实回测仍按原契约推进。
