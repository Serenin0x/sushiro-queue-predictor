# 云端独立接入：本次实施范围

2026-10-12 E0462。此页是可审阅的实施准备，**尚未授权／执行新的云端账号访问或材料上传**。已有公开归档和统计页面保持；新的官方数据通道此前仍在Mac。本人本轮明确继续，原目标保持active。

## 为什么需要这次迁移

本轮对原两个明确PID的只读检查未找到进程；最后持久事件仍是10月11日三店57／57完成。原内存审计材料及跨日调度不能当成仍然可用。没有据此重启、重读181包、重置旧采集或清除匿名403停止状态。云端需要有独立保存的初始化材料和会话，才能在电脑断网、休眠或关闭后继续。

## 固定目标与制品

- 仅本人已租的上海Ubuntu服务器 `124.222.57.236`，新独立目录 `/home/ubuntu/sushiwait-official-cloud-e0462-20261012`。原公开18080统计地址保持。
- 应用为本项目已校验的rc82制品：SHA256 `7edd729462a2692ab657a90da7fa4d3f577f7729b0248a4e6cc83be3764e62cb`；不用浮动下载覆盖旧服务。
- YYB为[准备好的Compose](../deploy/yyb_cloud_trial.compose.yaml)中的固定linux/amd64镜像摘要；新云会话只在该新目录保存。仅回环18081、uid10001、512MiB／1CPU／64PID、只读容器根和关闭容器日志。不是把本人Mac账号数据库复制上云。
- 三项Python依赖按[指定Linux制品及SHA](../deploy/official_cloud_requirements.cp312-x86_64.txt)安装到独立venv。官方[PyPI cryptography发行资料](https://pypi.org/project/cryptography/50.0.2/)提供其版本与文件摘要；本机元数据读取成功不等于云端安装完成。

## 需要新增明确授权的材料范围

只重新读取此前已定位、摘要固定的**寿司郎官方181版同一个代码包**一次，正文上限 **2,056,133字节、8秒**。不扫描目录、不读聊天、账号数据库或其他小程序，不运行官方代码。准备好的本机原创程序仅解析AST，核对既有参考日期头和客户端，再把本次正常初始化所需材料制成最多16KiB的私有JSON。

材料包括初始化签名参数、日期头AES参数／日期拼接规则、固定启动字段、应用客户端、设备缺省字段、版本及必要User-Agent／Referer。原“不输出／上传材料”的许可没有覆盖本次云端存放，因此必须由本人明确覆盖这一新增范围。新JSON只经SSH加密传到上述本人服务器，目录0700、文件0600；不出现在工具输出、日志、GitHub、公开统计页或大模型请求里。不上传原查询JWT、手机HAR、Mac微信／YYB账号数据库。

本机输出只报字节数、时间和成功状态。云端[固定接收程序](../deploy/official_material_receiver.py)最多8秒／16KiB，逐项核对预装的目标／181／包SHA；只首次创建 `initialization-material.json`，已有文件拒绝读取或覆盖。接收后持久状态不确定会明确报告committed与durability，不自动重传。

## 临时访问与首次本人认证

原公开归档SSH key保持原用途、禁止shell／转发和原期限。另建两把专用临时key，均在 **2026-10-14 22:00北京时间**到期（UTC `20261014140000Z`）：

1. 上传key用于现有ubuntu用户，`restrict`＋固定接收命令；不接受调用者命令、SFTP、shell或转发，只接收上面的一份JSON。
2. UI key用于新专用系统用户 `sw-cloud-ui`。只有服务器回环 `127.0.0.1:18081` 的本地转发；禁止远程／Unix socket转发、shell、TTY、代理、X11、用户启动脚本和隧道设备。具体[SSH配置](../deploy/official_cloud_ui.sshd.conf)在安装前必须由云端 `sshd -t` 和指定用户的 `sshd -T -C` 实测确认；目前只是文件准备。OpenSSH的[配置文档](https://man.openbsd.org/sshd_config.5)说明本地转发、PermitOpen与会话数量的限制。仅靠key的port-forwarding选项不能声称禁掉远程转发。

不新开公共端口，不给key命令行管理权限。私钥留本机，服务端只安装公钥；连接固定主机身份。访问期限结束后停止试验软件，私有材料／会话保留为私有交接资料，未授权删除。

本人通过这个回环通道完成云端控制台的正常首次注册／登录、微信扫码及设备认证；密码和设备确认由本人操作。新云会话可能触发上游重新验证或影响本机供应，需要如实观察，不能保证之后永久无需人工身份验证。

## 本次有限运行计划与完成判据

新云试验最多中关村大融城3014、西单大悦城3004、成都世豪2009三店；10月12–14日，每店900秒，默认营业11:00–22:00，整体10月14日22:00停止。最多每日44轮／132普通GET，三日396普通GET，加最多42次初始化后的单店验证GET；总上限438 GET。若当天晚启动，只记录实际窗口和缺口，不补造之前节点，不把新试验算作原Mac进程接续。新的期限／预算另写持久计划，原Mac两日计划不延长。

首次单店初始化、查询与完整私有上下文revision1持久化成功后，才启动三店采样。失败停止，401／403不循环更换身份或绕过拒绝；重启保留预算、游标和未知槽位。旧147匿名采集的halt保持，已保存数据不动。

已准备[有限控制程序](../deploy/official_cloud_controller.py)：私有计划固定上述日期、门店、900秒和42次更新上限；每次供应前先保存不可覆盖的检查点，进程中断后保留未知更新结果，不能直接重新申请。首次启动用schema2设置，既有上下文更新用单独schema1设置；需要在首次扫码后分别制备 `bootstrap-settings.json` 和 `renewal-settings.json`。子采集在每次GET前另有营业门控，关门前16秒保守停止，父进程调度延迟也不能直接放行。原子读者等待完整新上下文，按原collection任务恢复，不补赶请求；失败保留halt。独立的[云内公共同步](../deploy/official_cloud_public_sync.py)只读取正常化记录，不接触凭证或外网；原三表归档完整提交后才保存游标，断线后的原公共包可以幂等核对。公开同步截止10月14日22:02，以便存下停止采集前已完成的记录，没有新增来源查询。

systemd[采集模板](../deploy/official_cloud_trial.service.in)与[公共同步模板](../deploy/official_cloud_public_sync.service.in)已准备，未安装／启用；前者按持久计划运行，后者使用独立网络命名空间。E0463另补[供应器启动模板](../deploy/official_cloud_supplier.service.in)、[有限停止服务](../deploy/official_cloud_stop.service.in)与[到期定时器](../deploy/official_cloud_stop.timer.in)。启动调用已预拉取的固定Compose、不构建／拉取新镜像；会话目录保持，不能把模板存在当成云会话重启已通过。新的[时间守卫](../deploy/official_cloud_lifetime.py)拒绝三日试验之外的启动，只有服务器时钟正确才成立。固定UTC定时器在10-14 22:00北京时间停止新的采集及供应器，公开归档同步独立留到22:02，不删除会话、材料或已保存数据。

供应器service通过root访问Docker，本机不执行该模板；云端安装仍在本次具体软件／账号访问授权范围内。采集使用BindsTo和After关联供应器，供应服务停止时停止采集；供应器不循环自动重启。定时器Persistent保留错过的停止任务，不能代替逐GET营业保护或正确时钟。语义依据systemd项目的[unit文档](https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.unit.xml)、[timer文档](https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.timer.xml)与[service文档](https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.service.xml)。实际云端须先确认`docker compose`及固定镜像，再执行`systemd-analyze verify`、`systemd-analyze calendar`，核对四个service和停止timer的关联、实际期限与启用状态；尚未做这些实测。

扫码／私有配置制备、云端unit实测和实际页面接线仍待部署。合成控制、接收及真实SQLite归档／重放边界新增28项、原归档13项共41项已通过；E0463另有2项启动时间边界检查通过，未为纯模板重复旧全套。不是云重启或长期稳定性证明。

接入原三表公共归档和统计页后，要取得电脑离线仍有新记录、同一云读者跨真实声明期限、云会话／来源重启恢复的证据。真实安装和观察尚未完成，不能把准备文件或合成检查写成已经独立采集。详见[阶段目标](CLOUD_OFFICIAL_SUPPLY_PLAN.md)和[四板块交接](PROJECT_HANDOVER.md)。
