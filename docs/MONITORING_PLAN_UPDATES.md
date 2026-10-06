# 运行中的共享监控计划更新

开发候选0.2.0rc40新增`monitor-plans-publish`，以及`remote-window-collect`、`remote-window-serve`的显式`--plan-updates-file`。完整背景、需求、验证和操作历史见[交接文档](PROJECT_HANDOVER.md)。它更新后台的共享采集计划，不估计叫号、取号或发送提醒。

## 输入与发布

原`--plan-file`继续不可变，摘要仍属于配置；不提供新选项时原schema2的规则不变。新选项仅用于新建schema3任务，不能升级正在运行的旧任务。更新信封提供整个计划集合，替代而不合并种子计划；清空`plans`表示恢复已选门店背景采集，不停止整个任务。

输入和更新文件均需本人明确指定、16KiB以内、0600或0400、单硬链接，直接父目录0700，不跟随祖先或文件符号链接。更新文件必须处于独立私有目录，不能与数据库或任务共用父目录，以免与运行中的数据库目录锁冲突。示例只有合成标识和空计划，操作者应生成新的规范小写UUID4，并将日期改为当下不晚于发布时刻的声明：

```json
{
  "schema_version": 1,
  "series_id": "14c3094e-64bd-4c15-8d7c-7ea3ca54fba3",
  "revision": 1,
  "declared_at": "2026-10-07T10:00:00+08:00",
  "document": {"schema_version": 1, "plans": []}
}
```

`series_id`区分计划系列，不是身份认证或微信凭证。`revision`是1–2147483647递增整数；声明带时区且同系列不倒退，不证明用户实际操作时间。`document`沿用[共享策略](SHARED_MONITORING.md)：最多128份计划，固定1–3店，到店时间、叫号偏移、计划状态及明确加速输入；非空`last_poll_started_at`拒绝，实际查询进度只由任务保存。

以下是已准备私有目录和输入后的示例，不会创建真实用餐计划：

```sh
sushiwait monitor-plans-publish --input /private/tmp/queue-input/next.json \
  --output /private/tmp/queue-plans/current.json \
  --store-id 3014 --store-id 3004 --store-id 2009 --base-interval 300

sushiwait remote-window-serve --db /private/tmp/queue-task/remote.sqlite3 \
  --task-file /private/tmp/queue-task/task.json \
  --plan-file /private/tmp/queue-task/seed.json \
  --plan-updates-file /private/tmp/queue-plans/current.json \
  --store-id 3014 --store-id 3004 --store-id 2009 \
  --base-interval 300 --duration 86400 --max-pairs 8640
```

后续编辑整个输入集合和更高revision，再运行同一发布命令。发布者用非阻塞目录合作锁核对系列/版本/声明时间，完整写入0600临时文件并回读核验，文件确认后原子替换并确认目录。提交前失败保留旧文件；替换后目录确认失败明确返回`committed=true,durability_confirmed=false`和非零退出码，不回退或重试。POSIX替换没有真正的比较交换，合作锁不能阻止同OS用户非合作进程竞态。

## 采集者怎样应用

新schema3任务保存最新系列/revision、完整信封摘要、声明时间、本机应用时间和未观察版本数量。应用/任务时间保存毫秒下界，声明保留完整精度用于摘要及倒退检查；检查点先后比较按其毫秒分辨率，读取/接受时仍以当下完整时钟拒绝未来声明。更高版本在成对查询对账完成后接受，不拆开队列/数量一对；截止时间、请求预算、各店实际开始时刻、游标、结果链保持。恢复仍核对已保存结果，不重查，保守等待当前完整周期。

等待时最多每秒检查一次本机文件，**不会每秒查询门店**。接受更新后按同店最紧迫需求重新计算60／30秒或背景间隔，以实际开始时间锚定；同店多人共用一对查询。若缩短周期后当前目标已到，只抓一次现在，不追赶过去。系统调度、在途请求和提交可能延后检查，1秒是本机等待检查目标而非端到端延迟保证。UTC、单调时钟、原截止/预算和失败首停沿用[持久窗口](ANONYMOUS_WINDOWS.md)。

同版本内容变化、回退、系列变化、声明倒退/未来、越范围、损坏、不安全或缺失文件都停止，不回用旧配置假装成功。合法原子替换可能恰好落在身份核对间；只有该身份变化最多额外读一次本机文件，其他错误及HTTP不重试。直接连续写坏文件仍可能停采，应使用原子发布入口。

只观察到revision1和5时明确记3个未观察版本；首次接入5也记此前4个未知版本，不虚构历史。`complete_plan_history_verified=false`；当前文件/检查点不是完整用户操作日志，多用户认证、逐条计划变更日志和用户提交确认仍待实现。完成/失败任务不会因新计划复活或重写，也不能延长期限或绕过总预算。

## 状态与实际验证

`remote-window-status`只读检查点，schema3额外给出是否启用更新、接受revision、未观察数量；不读更新文件、不联网、不认证进程存活，不公开系列、摘要、私有路径或个人到店时间。号码服务显示最后公布检查点，可能落后本机接受动作。HTTP展示路由保持只读，未开放匿名写接口。

本轮29项专项、完整1069项软件检查通过，覆盖运行中60→30秒→背景、多人共享、在途对账、重启、终态、冲突、原子发布和质量工具兼容。首次24项有一项测试调用不存在的`activate`方法；改为实际`start`入口及双参数等待回调后通过，不放松生产守卫。合成时钟/运输和独立安装不替代真实来源验收。原三店24小时任务仍为rc31/schema2，不更新、不重启、不混合计数；安装/公开精确提交结果另记交接文档。

源身份/数量单位/新鲜度unknown、`eta_available=false`、真实训练标签0。尚无真实叫号预测、误差校准、防过号提醒、自动跳号风险、多用户计划API或取号/取消/重排；明确加速输入不等于真实过号率。本轮也未搭建或发布前端。微信小程序能够接入后台服务（[腾讯官方说明](https://docs.cloudbase.net/run/develop/access/mini)）；仍按R18先完成真实字段/刷新验证，再实现前端，软件检查不能替代全天、全国或生产服务器验收。
