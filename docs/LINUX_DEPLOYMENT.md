# Linux 采集与号码服务部署

候选 0.2.0rc37 提供 Docker 构建和 Compose 配置，将匿名采集、持久窗口和只读 HTTP 展示一起部署。软件检查、实际 Linux 容器、真实上游查询和云服务器是不同证据，实测追加于[交接记录](PROJECT_HANDOVER.md)。目前仍是最多三店、最多 72 小时的有界窗口，不称全国、无限守护或微信小程序已经上线。

## 构建与运行

需要操作者自己的 Linux 主机与 Docker/Compose，从仓库根目录执行。不用上传本人微信上下文、HAR、账号或手机。基础固定为 2026-10-06 实际解析的官方 Python 多平台摘要，构建与服务器依赖固定版本及 wheel SHA256。根目录 `.dockerignore` 只允许原创包源码、日历、README、MIT 许可、版本和安装配置；不传 `.git`、真实数据、凭证或设计试验。

```sh
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml up -d
curl --fail http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/api/v1/status
curl http://127.0.0.1:8765/api/v1/stores/3004/queue
```

默认三试点 3014/3004/2009，基础 300 秒、24 小时、900 对预算（最多 1800 GET）、空计划。运行会真实查询官方匿名来源；参数不代表上游长期使用许可或频率保证。两条 GET 非原子，原始 count 单位、返回门店身份和源更新时间未认证，不提供 ETA 或真实过号率。

容器进程 UID/GID 10001、只读根、去除 capabilities、禁止获得新权限。宿主只发布 `127.0.0.1:8765`；容器内显式 `--listen-host 0.0.0.0` 供桥接访问，不等于开放公网。普通命令默认仍是 127.0.0.1。保持单工作者、禁代理头、无访问日志与三条只读路由，查看号码不增加上游查询。

## 保存状态与重启

命名卷 `queue_state` 保存 `/state`，初次复制镜像中的 0700 目录与 0600 空计划，数据库、任务及锁属于同一 UID。不要挂载本人主目录、同时运行两个写者、删除运行中的任务文件或改变计划内容。复制/搬迁数据库会改变文件身份，本版没有跨主机迁移或备份恢复工具。

```sh
docker compose -f deploy/compose.yaml stop
docker compose -f deploy/compose.yaml up -d
```

显式 `--resume-if-present` 在独占任务锁内选择：存在任务则校验并恢复，保留原截止、预算、已保存结果、未知槽位及完整链；任务和数据库都不存在才新建。已有数据库却缺任务时停止，预检后出现数据库也由排他创建拒绝。配置、计划、身份或历史冲突即停止。旧默认拒绝覆盖与 `--resume-task` 保持，两恢复选项互斥。

恢复后等当前完整周期，不补发历史请求。失败、完成和到期不重新查询；可继续展示 `saved_history`/`last_known_only`，健康检查 503。Compose 不自动重启，健康检查不创建下一窗口；目前没有永久轮换、动态计划或失败恢复。停止后保留卷，不使用删除数据的 `down -v`。新验收窗口须明确安排独立状态和预算，保留旧结果，不以循环删库重开消除失败。

## 检查与云端验收

```sh
python3 scripts/check_container.py --image sushiwait:0.2.0rc37
```

检查真正启动 Linux 容器、通过宿主 HTTP 读取、停止并重建三次。上游传输明确合成：虚构门店 900001，两对/四次假请求，官方请求零；它不是实际门店或预测验证。核对私有权限、回环绑定、共享读取、恢复周期、原期限/预算、终态无查询与文件不变，最后只清理自己的测试容器与卷。合成启动脚本不进入部署镜像，不改变正常采集程序。

新增[手动云端来源检查](CLOUD_SOURCE_CHECK.md)，固定三店各一次、最多6次真实GET，只公开聚合结果；与普通合成CI分开，实际结果按运行ID和提交记录。

Docker Desktop Linux 虚拟机出口仍来自本机，不代表指定云服务器可访问官方源。云端上线还需明确主机，核对 TLS/DNS/网络、24/72 小时及跨日期质量、缺口、磁盘与备份，再配置经验证的 HTTPS 域名、访问控制和告警。微信前端及发布按 R18 和平台配置另验；本轮未租服务器、注册 AppID、开放公网或上线界面。

依据：[Docker 运行与持久卷](https://docs.docker.com/engine/containers/run/)、[构建上下文](https://docs.docker.com/build/concepts/context/)、[Uvicorn 部署](https://uvicorn.dev/deployment/)。非 root 容器进程不等于 Docker 守护进程采用 rootless 模式；本项目未修改操作者 Docker 权限。
