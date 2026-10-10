# DS-004 云端显示部署

2026-10-10 14:09:37 UTC+8，新月历已在[原统计地址](http://124.222.57.236:18080/statistics)上线。城市／门店、月历、单日曲线和原号码读取现有统计副本；旧号参考与用餐记录位于 `/statistics-legacy`。完整事实见 [E0392](PROJECT_HANDOVER.md)，设计交付见 [DS-004](design/integration/FRONTEND_HANDOFF.md)。

## 实际运行版本

| 部分 | 实际版本／位置 |
| --- | --- |
| 采集与内部 hub | 原 rc64／081f56d，原解释器和代码未替换 |
| 显示基础 | 从服务器已安装的 rc64 包复制到独立显示目录 |
| 显示更新 | 723677b54b635864bc98d353cb0c84b452f61fee 的 MonitorHub、网关、7新资源与7保留资源 |
| 显示目录 | `/home/ubuntu/sushiwait-frontend-ds004-20261010` |
| 解释器 | `/home/ubuntu/sushiwait-rc64-src/.venv/bin/python` |
| 统计配置 | `/home/ubuntu/sushiwait-mainland-daily-20261010/hub.json` |
| 公开入口 | 原 TCP 18080 的 `/statistics` |

显示目录的 `serve.py` 在隔离解释器内选择 `runtime`，再执行该目录的 `statistics_gateway.py`。不是整套 rc70 算法、命令行或采集升级；目录仍保留基础版本 rc64，避免错误声明。

## 准备与切换

固定提交的 [资源清单](design/integration/frontend-resources.json) SHA256 为 `4730e8fe1ba89baa7c00b1c8cca657faf97f816fd4cbe2456e85fd64894feb0a`。16份下载文件均按固定来源和 SHA 校验，下载使用正常 TLS；全部校验成功后才新建显示目录。原安装包复制排除缓存文件，没有读取实际凭证。目录0700／文件0600，私有部署清单保留每个文件的摘要。

准备检查通过7份新资源的固定入口、MIME与SHA；替代读者遇到任何工作节点读取都会报错，静态检查未触发节点或餐厅查询。

只增加现有显示服务的 drop-in：

```ini
# /etc/systemd/system/sushiwait-mainland-statistics.service.d/40-ds004-display.conf
[Service]
ExecStart=
ExecStart=/home/ubuntu/sushiwait-rc64-src/.venv/bin/python -I /home/ubuntu/sushiwait-frontend-ds004-20261010/serve.py --config-file /home/ubuntu/sushiwait-mainland-daily-20261010/hub.json --listen-host 0.0.0.0 --port 18080
```

保存原unit，重新载入配置，仅重启 `sushiwait-mainland-statistics.service`。切换前后 collector MainPID 都是367596；hub、原数据、任务期限／预算、防火墙保持。既有显示服务的权限隔离继续生效。

## 实际检查

- 原公开地址7资源 SHA、MIME、`no-store` 与本地资源／同源连接 CSP 一致，旧页200。
- `/health`、`/api/v1/status`、`/monitor`、`/deployment.json` 仍404。
- 西单3004／2026-10-09的660点JSON切换前后逐字一致，SHA256 `47b0cf034d77be8324f50f9a6b5be0a4875a0518444820e15353a9202c16e7ba`。
- 浏览器实际完成全国月历→北京→西单→昨日详情；真实号码曲线、变化速度、单条数量和最近60组可读。末次空列表没有被较早号码替代；控制台没有warn／error。

公开检查时间为14:10:04 UTC+8。此前源码与设计检查不重跑；本次以服务器安装和原地址的实际结果完成显示部署。

## 回退

仅回退显示服务：将新增 `40-ds004-display.conf` 移到私有显示目录的未启用位置，保留该文件；重新载入systemd并重启statistics服务。原unit没有被覆盖，移开新增drop-in后使用原rc64入口。核对原页、API和collector PID，不重启采集／hub，不修改日库、失败保护或预算。

仅进入旧功能不需服务回退：直接打开 `/statistics-legacy`。

## 当前数据状态

来源在13:53:29收到403而保护停止，早于显示切换；当前页面可读已保存资料，不代表实时采集已恢复。详细事故与剩余问题见 [来源拒绝处理](SOURCE_DENIAL_REVIEW.md)。部署和来源恢复是两个独立结果。
