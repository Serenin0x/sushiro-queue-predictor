# Linux 部署中修改共享监控计划

候选0.2.0rc41为Docker部署新增独立私有计划卷与管理员入口。程序在后台运行时，操作者可以提交新的整个关注集合，不需要自己填写UUID、递增版本或声明时间。它沿用[运行中计划更新](MONITORING_PLAN_UPDATES.md)的schema3、原子发布、同店共享和原截止/预算规则。完整操作与验证追加于[交接文档](PROJECT_HANDOVER.md)。这不是微信用户界面或多用户写入API。

## 创建独立部署

从仓库根目录执行，需要自己的Docker主机。此配置会查询三家真实试点3014/3004/2009，背景300秒、24小时、900对/最多1800GET预算，与[基础部署](LINUX_DEPLOYMENT.md)一致。使用独立项目名；不能用下面的覆盖配置升级已有schema2状态卷。启动前初始化计划卷，初始化容器只访问本机文件，不查询门店：

```sh
docker compose -p sushiwait-live-plans \
  -f deploy/compose.yaml -f deploy/compose.plan-updates.yaml build

docker compose -p sushiwait-live-plans \
  -f deploy/compose.yaml -f deploy/compose.plan-updates.yaml \
  run --rm --no-deps --entrypoint python collector \
  -I /opt/sushiwait/plan_admin.py init

docker compose -p sushiwait-live-plans \
  -f deploy/compose.yaml -f deploy/compose.plan-updates.yaml up -d
```

`queue_state`映射`/state`，保存原计划种子、数据库和检查点；独立`plan_updates`映射`/plan-updates`，避免碰运行中的数据库目录锁。两卷目录0700、文件0600、UID10001，容器根只读，HTTP仍只发布到宿主127.0.0.1。镜像内的`/opt/sushiwait/plan_admin.py`属于部署工具，不新增wheel模块或默认依赖；镜像不包含合成测试脚本、凭证或真实观测。

`init`仅在文件不存在时生成新的UUID4和revision1空计划。已存在且合法则只显示安全摘要，不重置、不修复损坏文件；未初始化时`publish`、`clear`、`status`拒绝操作。不要删除已有文件来解决错误，不用`down -v`清除历史。

## 修改、清空和查看

准备本人指定的私有JSON文件，沿用[共享策略](SHARED_MONITORING.md)的schema1 `document`，最多128计划、固定三店。以下时间仅演示格式，发布时改为实际希望到店的带时区时间；这不是已经预约或已经取号：

```json
{
  "schema_version": 1,
  "plans": [
    {"store_id": "3004", "desired_arrival_at": "2026-10-07T19:00:00+08:00"}
  ]
}
```

输入是整个替换集合，不自动与旧计划合并，也不允许借用`last_poll_started_at`。相同门店多份计划共用采集，取最紧迫60／30秒需求；普通时段回到300秒，清空集合继续背景采集。发布时间和后继revision由工具按当下时钟生成，原任务期限与总预算不变。用户希望叫号与到店的正负偏移语义继续见共享策略，本入口不计算预测或操作真实票据。

```sh
docker compose -p sushiwait-live-plans \
  -f deploy/compose.yaml -f deploy/compose.plan-updates.yaml \
  exec -T collector python -I /opt/sushiwait/plan_admin.py publish \
  < /absolute/private/desired-plans.json

docker compose -p sushiwait-live-plans \
  -f deploy/compose.yaml -f deploy/compose.plan-updates.yaml \
  exec -T collector python -I /opt/sushiwait/plan_admin.py clear

docker compose -p sushiwait-live-plans \
  -f deploy/compose.yaml -f deploy/compose.plan-updates.yaml \
  exec -T collector python -I /opt/sushiwait/plan_admin.py status

docker compose -p sushiwait-live-plans \
  -f deploy/compose.yaml -f deploy/compose.plan-updates.yaml \
  exec -T collector sushiwait remote-window-status --task-file /state/task.json
```

管理员输出只含版本、数量及提交状态，不输出UUID、文件路径或个人到店时间。`committed=true`表示计划文件已经替换，`worker_application_verified=false`明确它不是采集者确认。检查点的`accepted_plan_revision`才表示采集者已接受到哪一版；HTTP状态可能仍是上一次公布的检查点。等待期间最多每秒检查本机文件，不每秒请求门店，不保证端到端一秒响应。

输入最多16KiB，重复键、非有限数、损坏、越店范围、不安全文件、未来声明、倒退或同版本冲突拒绝。发布前失败保留旧文件；发布后目录持久化不能确认时返回非零退出、`committed=true,durability_confirmed=false`，不能把它解释为未提交并盲目重试。合作锁限制合规发布者，不能防止同OS用户直接篡改文件。管理员必须拥有本机Docker操作权限，本版没有微信身份映射、多用户授权、完整计划操作日志或公网写入口。

已完成或失败的任务即便收到新计划也不复活、不延长期限，旧检查点保留。重启继续使用同项目、同参数和同卷；真正的新窗口需要明确的独立状态和预算，不能循环删库。实际生产主机、HTTPS访问、可靠通知和备份恢复继续单独验收。

## 合成来源的实际容器验证

```sh
python3 scripts/check_container_plans.py --image sushiwait:0.2.0rc41
```

检查启动真实Linux容器和HTTP，使用实际墙钟/单调时钟及本机管理员发布；固定合成店900001和假传输，官方请求0。它核对初始空计划、同店两计划、运行中60／30秒与清空、已接受revision、终态的新文件版本不复活任务、重建后库/检查点/种子字节保持，并只清理本次独有的容器、测试镜像与卷。合成计划及假响应不作为真实用餐、过号、预测误差或长期稳定性的证据；实际运行结果以交接编号为准。

## rc41本机验证结果

2026-10-07完整1087项12.531秒通过，43模块包及部署镜像逐项核对，管理员工具和README元数据一致。实际动态容器125.057秒，合成3对6GET/官方0，实际60.001/30.016秒；运行中接受revision1–4，结束后的新文件revision5不复活任务，重建后的DB/检查点/种子不变、健康503、worker已结束。独立静态容器64.131秒/恢复60.012秒、合成2对4GET分别通过；两个测试不混计。它们验证实际Linux容器行为，不替代真实来源、全天或生产服务器验收；细节见交接E0274。
