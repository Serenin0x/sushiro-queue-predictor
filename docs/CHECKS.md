# 自动检查与安装验证

工作流为 [.github/workflows/checks.yml](../.github/workflows/checks.yml)，本机安装包检查入口为 [scripts/check_installed_package.py](../scripts/check_installed_package.py)。每次 main 推送、针对 main 的普通 pull_request，以及显式 workflow_dispatch 运行离线检查；没有定时采集、业务操作、部署或凭证更新任务。

Ubuntu 24.04 的 Python 3.11、3.12、3.13 三组分别执行完整检查，Node 22 执行既有适配器场景。每组再构建 wheel、安装到临时虚拟环境，在 checkout 外用 `-I` 运行安装验证：核对版本与实际导入位置、CLI 帮助、合成回放及只读报告；rc2另核对合成结果校验/私有追加/汇总，真实性/标签资格仍为未验证。回放/报告及结果路径将 socket 调用设为失败，数据库只在临时目录；CLI 帮助子进程单独核对退出状态。它不访问寿司郎，不读 HAR、微信数据或本机凭证，也不把合成测试算作 live 验收。

权限仅 contents:read，checkout 不持久化授权；没有项目 secrets、OIDC、发布权限、pull_request_target、生产服务器或 artifact 上传。测试使用标准库；构建所需 setuptools 由标准构建隔离取得，项目运行依赖仍为空。工作流十分钟超时，较旧同分支检查可取消；这不是采集器的停止/重试策略。

2026-10-04 按官方仓库实际标签解析到不可变 commit，并核对该 commit 的 action.yml 输入：checkout v7 `3d3c42e5aac5ba805825da76410c181273ba90b1`，setup-python v7 `5fda3b95a4ea91299a34e894583c3862153e4b97`，setup-node v6 `249970729cb0ef3589644e2896645e5dc5ba9c38`。动作自身使用 Node 24；项目适配器测试另用 Node 22。后续更新须重查来源与实际 CI，不把可变标签自动漂移作为验证。

来源：[GitHub 工作流语法](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)、[checkout 固定动作](https://github.com/actions/checkout/blob/3d3c42e5aac5ba805825da76410c181273ba90b1/action.yml)、[setup-python 固定动作](https://github.com/actions/setup-python/blob/5fda3b95a4ea91299a34e894583c3862153e4b97/action.yml)、[setup-node 固定动作](https://github.com/actions/setup-node/blob/249970729cb0ef3589644e2896645e5dc5ba9c38/action.yml)。本机、GitHub 实际运行及真实接口结果分别记录在 [V0_2_ACCEPTANCE.md](V0_2_ACCEPTANCE.md) 和 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md)。

2026-10-04，提交3279059的[实际工作流运行](https://github.com/Serenin0x/sushiro-queue-predictor/actions/runs/37196578650)已completed/success，三组均261项检查及构建/checkout外安装通过。普通HTTPS推送因既有PAT缺workflow scope被拒绝，远端当时未移动；使用现有GitHub连接以force=false更新同一提交后成功，不新增凭证或权限授权。完整失败/发布核对见编辑历史E0065；这些离线成功不证明真实续期或服务器可持续运行。

rc2构建时按[PyPA许可元数据格式](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#license-and-license-files)采用SPDX MIT/显式LICENSE，最低构建后端setuptools77.0.3；运行依赖仍0。最终本机wheel13个模块与src一致，checkout外合成结果路径也通过socket0检查。283项本机已通过；rc2提交a728bf93cb4e1b5dd98de97d02cebe06461aa97c的[实际运行37199591656](https://github.com/Serenin0x/sushiro-queue-predictor/actions/runs/37199591656)completed/success，Python3.11、3.12、3.13分别283项/2.596秒、2.553秒、2.291秒，均OK，三组构建/checkout外安装步骤success。结果路径socket0、已验证训练标签0；未发生产请求，没有正式版本或部署。

已公开rc3新增15项日期检查，完整298项/1.741秒通过；安装检查增加date-features及2026固定JSON包资源，从checkout外验证调休上班日、营业未知/ETA不可用及socket0。最终本机wheel/14代码模块及JSON一致、checkout外安装/date-features通过socket0；提交d7897ed的[实际GitHub运行37200713068](https://github.com/Serenin0x/sushiro-queue-predictor/actions/runs/37200713068)三组298项及构建/安装/包资源检查全部success，实际日志核对公共/结果/日期路径socket0；工作流动作/权限/输入保持。

已公开rc4新增[窗口分析](SIGNALS.md)19项/0.077秒，完整317项/1.944秒OK、无跳过。包含实际合成SQLite并发追加时的一致读取、未来/缺口/失败/逆序防污染、只读DB1和CLI零网络/客户端/凭证；既有26个JS场景已含在wrapper，不重复计数。最终离线wheel65,923字节、SHA256 `7bcbe5c255f1581cbf57369dd519b886af21ae174ee509ea285cca33405cd4c8`，15模块/年度JSON/README元数据与源码一致，MIT/运行依赖0。checkout外独立安装入口通过，signal-report及公共/结果/日期四条检查路径socket0；安装检查脚本新增实际信号输出资格核对，工作流本身未改。rc4提交829c392的[实际运行37204416416](https://github.com/Serenin0x/sushiro-queue-predictor/actions/runs/37204416416)completed/success：Python3.11为317项/2.546秒、3.12为317项/2.206秒、3.13为317项/2.927秒，三组构建/checkout外安装及四路径socket0全部通过，76远端blob一致；没有生产请求或真实恢复。

本轮rc5增加可选接收诊断，完整324项/4.111秒OK、无跳过；新增3项合成回环和4项零网络CLI检查，区分连接/鉴权/帧格式/观测与固定拒绝原因，默认输出保持。原26个JS场景计入wrapper。运行/安装/远端CI以新版本实际编辑记录为准，不套用rc4结果；首次真实诊断有效投递0，不是自动续期或恢复成功。

rc5最终离线wheel为67,262字节、SHA256 ae8804d35da1f50580fcb13da8836903e8c3dc09d8a57c971337c28e5e3dd5ea；15模块、2026 JSON及当前README元数据逐一匹配，MIT/运行依赖0。官方Python3.12.14/setuptools84构建，在checkout外独立venv/隔离入口通过CLI帮助与诊断选项、公共/结果/日期/信号四条socket0检查，verified_training_labels=0。 GitHub实际新版本CI结果在随后发布审计单列。

rc5代码197d465的[实际GitHub运行37265092281](https://github.com/Serenin0x/sushiro-queue-predictor/actions/runs/37265092281)已completed/success；Python3.11、3.12、3.13分别324项/4.883秒、4.748秒、4.800秒，全部构建/安装步骤成功，实际日志确认版本rc5、诊断选项和四路径socket0。132远端blob逐一一致，详见E0082；没有真实新凭证独立GET或恢复。
