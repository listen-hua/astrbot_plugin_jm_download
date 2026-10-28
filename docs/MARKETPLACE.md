# AstrBot Cloud 提交资料草案

此文件用于提交审核，**不表示已收录**。按 [官方发布指引](https://docs.astrbot.app/dev/star/plugin-publish.html) 在 AstrBot Cloud 提交仓库及资料，审核要求和内容规则以提交时的现行规定为准。安装包上限 16 MB，本项目构建按 16 MiB 检查且实际包远小于两种口径的上限。

## 基础信息

- 名称：JM 下载
- 标识：`astrbot_plugin_jm_download`
- 作者：黎沈华(listen-hua)
- 版本：以仓库 `metadata.yaml` 为准（首个公开版本 v1.1.0）
- 仓库：https://github.com/listen-hua/astrbot_plugin_jm_download
- 许可证：AGPL-3.0

## 功能介绍

通过 `/jm编号` 调用 JMComic-Crawler-Python 下载 JM 站点整本作品，按原始章节和页码整理为 ZIP，发送回发起任务的 QQ 会话。支持可选 AES-256 解压密码、后台队列、并发与用户冷却、完整性检查及失败文件保留。无需 LLM。

用途涉及 JM 站点，可能包含成人漫画，不能描述为一般图片工具以隐瞒实际用途。项目不能保证符合官方市场的内容规则。[AstrBot EULA](https://github.com/AstrBotDevs/AstrBot/blob/master/EULA.md) 对色情等用途有限制；请据此如实提交，由官方决定能否接纳。仓库发布与市场审核是两个独立流程。

## 安装说明与平台要求

通过 AstrBot WebUI 的仓库链接安装，或上传 GitHub Release ZIP。需要 Python 3.12、AstrBot ≥4.29.3 且 <5，已连接的 QQ 官方 WebSocket / Webhook 或 NapCat OneBot V11 `aiocqhttp` 平台。依赖 `jmcomic==2.7.10`、`pyzipper==0.4.0`。

NapCat 必须能读取插件 ZIP，本机使用绝对路径，跨容器使用共享卷及 `onebot_remote_jobs_dir`。详见 [DEPLOYMENT.md](DEPLOYMENT.md)。默认所有可触达用户均可使用，受 AstrBot 和 QQ 平台权限约束；不捆绑任何账号凭据、内容或下载结果。

## 测试结果及提交状态

自动化检查范围、真实验证结果和仍未完成的 NapCat 群聊、私聊清单见 [COMPATIBILITY.md](COMPATIBILITY.md)。提交时附对应 Actions 链接，不将模拟接口测试描述为真实 QQ 验收。

- 发布资产：由版本标签触发工作流验证后生成 ZIP 与 SHA256SUMS。
- 包大小：构建脚本自动检查，提交时填实际 Release 资产大小。
- 真实 NapCat QQ 验收：待完成。
- AstrBot Cloud 审核提交：待维护者使用自己的账号提交。
