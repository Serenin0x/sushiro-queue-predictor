# 每日原始数据备份与恢复

`day-backup-create` 把一个明确的已闭店日期打包为私有 ZIP，包含完整匿名 SQLite 原始库、窗口及日任务证据、曲线投影和归档结果。完整数组与原始请求／响应／本机接收时间逐字保留；不只保存图中的前三位。此工具不联网、不打开官方小程序、不启动或恢复采集，也不覆盖原文件。

## 创建副本

输出的父目录须预先存在、属于当前用户并为0700；输入和输出文件为0600。明确逐店指定，重复店号、活跃写者、尚未闭店、坏归档、库与检查点不一致均拒绝，失败不发布部分ZIP。新日采集根目录格式为`store-ID/YYYY-MM-DD/`，旧有限试采使用`store-ID/campaign/`与`daily-exports/`，两者显式选择。

```sh
sushiwait day-backup-create --root /private/path/daily-root \
  --store-id 3014 --store-id 3004 --date 2026-10-10 \
  --output /private/path/backups/2026-10-10.zip

sushiwait day-backup-create --root /private/path/finite-trial-root \
  --store-id 3014 --store-id 3004 --date 2026-10-09 \
  --source-kind legacy --output /private/path/backups/2026-10-09.zip
```

工具取得既有日任务和窗口的写者锁，检查全记录摘要链及归档摘要，逐文件记录大小与SHA256，再发布不可覆盖的ZIP。每店都有独立一致性检查，不宣称各店在同一原子时刻采样。原有限试采最后一个窗口可在正常停采后保持active状态，但不得存在pending提交，数据仍须与原检查点一致；本工具不会重置旧期限或预算。源文件的固定描述符、私有权限及前后身份检查可发现替换，拒绝符号链接和共享硬链接。

## 实际恢复读取

把ZIP通过本人正常云控制台文件下载到另一台电脑，保持私有目录和0600权限。不能把它放入公开GitHub、公开静态资源、聊天附件或大模型请求。

```sh
sushiwait day-backup-check --input /private/path/backups/2026-10-09.zip
sushiwait day-backup-restore --input /private/path/backups/2026-10-09.zip \
  --destination /private/path/recovered/2026-10-09
```

check会在临时新目录解包并实际打开只读数据库、核对记录链与数量、重新读取日期曲线，随后清理演练目录。restore的目标必须不存在，验证通过才保留整个新目录；原采集根、既有恢复目录和原ZIP不改。重复成员、目录越界、链接、超限文件、额外文件、篡改摘要与空间不足均拒绝。备份格式1限制每文件64MiB、总解压2GiB、最多8192成员；新每日任务最多256明确店，旧试采最多16。

恢复内容是原历史证据，保留检查点原路径与身份，不复制控制器或写者锁，不允许从恢复目录继续原采集。新日曲线可用现有`read_day(恢复根/store-ID, ID, 日期)`读取，旧曲线用`read_legacy_day(恢复根/legacy-exports, ID, 日期)`读取；完整原数组仍在恢复的remote.sqlite3中。

`restored_readable=true`表示实际恢复读取通过；`independent_machine_verified=false`始终保留，因为程序无法自行证明两次命令运行在不同物理设备。异机证据另记录传输前后ZIP摘要、两端运行结果、日期与数据数量。仅在服务器另建目录不算异机备份；本工具不新购云存储、不创建公网下载接口或自动同步凭证。当前服务器原日库与同机归档持续保留，异机传输与实测结果以交接记录为准。
