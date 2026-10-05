# 私有有界采集任务与重启恢复

rc7增加`collect --task-file`、`--resume-task`和离线`task-status`。它保存一个明确的1–3店、30–3600秒周期、每店1–120轮任务的进度，**不是常驻服务、自动凭证提供方或全天调度器**。长期实施顺序见[LONG_TERM_COLLECTION.md](LONG_TERM_COLLECTION.md)，正常凭证来源见[SURGE_INTAKE.md](SURGE_INTAKE.md)。

## 任务与快照分开保存

公共快照数据库保持schema2，既有报告与来源隔离保持；私有任务文件是独立schema1、最多16KiB的JSON。它保存固定配置、数据库文件身份、已完成槽位、成功/失败/未知计数、待对账尝试、最后一份结果校验及凭证版本检查值。**不保存原始授权或上下文头。** 任务里含本机路径和私有校验值，仍不得上传、公开或传LLM；`task-status`仅输出允许的摘要，不显示路径/哈希/文件身份/运行标识，也不读取凭证或查询门店。

任务每轮按明确门店顺序各查询一次，一个门店在一轮中的查询是一个槽位。完成槽位不等于成功观测：失败和未知分别计数，只有全部槽位成功时`all_slots_successful=true`。缺少的实时结果不会补造。

POSIX现有父目录须归本人所有且0700，任务/数据库/常驻空锁文件须0600、单链接常规文件，路径祖先和文件不可为符号链接。不自动修复不安全权限。每个任务持有自己的文件锁，防止合作进程同时执行相同任务；锁文件结束后保留，不通过删除锁文件解除占用。不同任务共享门店的调度与全局去重尚未实现，不要为同一采样计划另开任务。

## 创建与显式恢复

凭证文件由现有正常接入流程提供；下面不生成凭证，也不会自动打开微信或Surge。须显式选择已核验门店，不能把合成示例ID用于真实查询。

```sh
SUSHIWAIT_TASK_DIR="$HOME/Library/Application Support/SUSHIWAIT/local/collection-trial"
mkdir -p "$SUSHIWAIT_TASK_DIR"
chmod 700 "$SUSHIWAIT_TASK_DIR"

PYTHONPATH=src python3 -m sushiwait collect \
  --api-profile miniapp_gateway --store-id 3004 \
  --credentials-file "$SUSHIWAIT_CONTEXT_FILE" \
  --db "$SUSHIWAIT_TASK_DIR/samples.sqlite3" \
  --task-file "$SUSHIWAIT_TASK_DIR/trial.task.private.json" \
  --samples 3 --interval 60 --wait-for-credentials 600
```

这是macOS路径示例，其他POSIX系统选择自己已有的私有目录；系统CA仍沿用[DATA_ACCESS.md](DATA_ACCESS.md)的正常校验。普通`collect`不传`--task-file`继续按原有有界方式运行，不能加`--resume-task`。任务模式必须用完整私有凭证文件，不用环境或匿名模式。

恢复时重用**完全相同**的门店顺序、profile、数据库路径、周期、目标轮数和凭证等待配置，并加`--resume-task`。凭证文件可由正常流程换新；其revision与整组一致性另行检查。没有恢复标志时已有任务拒绝运行，恢复标志下任务不存在也拒绝，不自动创建新库替代丢失的旧库。

```sh
# 将上面的同一条collect命令加上 --resume-task 后执行。
PYTHONPATH=src python3 -m sushiwait task-status \
  --task-file "$SUSHIWAIT_TASK_DIR/trial.task.private.json"
```

完成任务再次显式恢复只核对数据库/结果并报告状态，不读取凭证或查询；包含失败/未知槽位时返回1，不把它当全成功。任务停止后没有后台自动重启；401或其他请求失败仍在首次错误后停止，显式恢复继续下一槽位，不重放上一失败请求。401记住已拒绝的授权校验值，必须有正常的新授权才能继续；只增加revision仍不足。

## 中断与对账

查询前先原子保存尝试的门店槽位、当前SQLite run标识和已有最大样本ID。查询结果按原快照工具提交SQLite后，再完成任务进度。

- 已保存结果、尚未保存进度：恢复时找到同一run和边界之后的唯一对应结果，只计一次，不重查该槽位。
- 有待对账尝试、没有保存结果：记录未知槽位及中断时间范围，继续剩余槽位，不假定请求未发生或重发旧槽位。
- 多份候选、身份/profile/结果冲突、数据库被替换或最后结果校验变化：停止，不能猜测进度。
- 没有待对账尝试、只有停止期间的时间缺口：记录重启区间；未来从当下继续，不补发过去的周期。未完成任务在重启后等完整采样周期，包括一轮中的部分门店，避免追赶。

每次新进程使用新run标识，既有质量报告不会把跨进程的两份快照自动解释成连续观测。`last_gap`只保留最近一个恢复范围，成功/失败/未知累计计数保留；它不是完整服务事件日志、源更新时间或真实弃号事件。进程是否仍存活必须检查实际进程，不能只看任务文件的state。

## 凭证与磁盘边界

任务只私有保存整组上下文校验值与最高revision，跨重启拒绝版本倒退或同版本内容变化。声明保护触发后记住需要新授权；时钟回拨不能使旧保护授权重新可用，检测到任务时间倒退也停止。HTTP401的拒绝与声明保护分别记录，不把本机停止编成服务端状态或实际到期时刻。

文件写入用同目录临时文件、文件fsync、原子替换和目录fsync。提交前写入失败保留旧进度，不继续GET；替换后持久化确认失败明确返回`collection_task_durability_unconfirmed`，已经提交的任务不回退。再次启动按实际文件/SQLite对账。合作文件锁和身份检查不能保证同用户恶意修改永远不发生；手工备份/恢复应将任务和数据库作为配套资料核验，不能拿旧任务文件任意回滚当前进度。

当前实现仍有界、显式运行，未自动安装常驻进程、开机启动或云服务；新代码的模拟/进程中断检查和真实只读重启试验分别记录在[PROJECT_HANDOVER.md](PROJECT_HANDOVER.md)编辑历史。凭证自动更新、长期吞吐、上传/备份服务、全国覆盖、模型/前端/业务与正式v0.2/v1验收仍需逐项完成。
