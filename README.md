# AstrBot JM 下载插件

[![Verify](https://github.com/listen-hua/astrbot_plugin_jm_download/actions/workflows/verify.yml/badge.svg)](https://github.com/listen-hua/astrbot_plugin_jm_download/actions/workflows/verify.yml)

发送 `/jm123456` 或 `/jm 123456`，通过 JMComic 下载对应作品的全部章节，检查完整性后将 ZIP 发回原 QQ 群聊或私聊。支持大小写、章节与图片顺序整理、可选 AES-256 解压密码、后台队列和失败文件保留。**无需配置 LLM**。

作者：**黎沈华(listen-hua)**。许可证：[AGPL-3.0](LICENSE)。当前版本见 [metadata.yaml](metadata.yaml)，更新内容见 [CHANGELOG.md](CHANGELOG.md)。

## 环境与平台

- Python **3.12**，AstrBot **≥4.29.3 且 <5**；CI 固定验证 AstrBot 4.29.3。
- QQ 官方 WebSocket、QQ 官方 Webhook，或 **NapCat / OneBot V11（AstrBot 的 `aiocqhttp` 适配器）**。
- 固定运行依赖：`jmcomic==2.7.10`、`pyzipper==0.4.0`，由 AstrBot 安装。
- NapCat 必须能读取 ZIP 所在目录；跨容器部署需要共享卷和路径映射，见 [部署说明](docs/DEPLOYMENT.md)。

当前实际验证范围和待验收项目见 [兼容性矩阵](docs/COMPATIBILITY.md)。其他 OneBot 实现未经验证。

## 安装与升级

在 AstrBot WebUI 的插件管理页选择通过仓库链接安装，填入：

```text
https://github.com/listen-hua/astrbot_plugin_jm_download
```

也可以从 [Releases](https://github.com/listen-hua/astrbot_plugin_jm_download/releases) 下载 `astrbot_plugin_jm_download-v版本.zip`，在插件管理页选择本地 ZIP 安装。发布页提供 `SHA256SUMS` 用于校验。安装后配置机器人连接，确认插件已启用。

Windows、Linux 和 Docker 均优先使用以上方式。Windows 启动器手动安装脚本、共享卷示例和升级说明见 [部署说明](docs/DEPLOYMENT.md)。升级前备份 AstrBot 的 `data`；现有配置键保持兼容，新配置使用默认值。保存配置后重载插件，对新任务生效。

## 使用

1. 在目标群聊或私聊发送 `/jm编号`，例如 `/jm123456`。只接受单个正整数编号，`/jm` 返回帮助。
2. 立即收到受理或排队状态；后台下载全部章节，失败或不完整的作品不会发送。
3. 完成后在原会话接收 `JM编号.zip`；解压后为 `编号/0001/000001.jpg`，按名称升序阅读即可。

AstrBot 需要实际接收到命令。如配置了仅响应 @ 消息，使用 `@机器人 /jm编号`。所有用户默认可用，同时遵守 AstrBot 的白名单和平台权限。命令会停止后续 AI 回复；普通聊天仍由 AstrBot 自身的聊天配置处理。

## 配置

在插件配置面板设置，保存并重载：

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `max_concurrent` | 1 | 全局同时处理任务数，1～8 |
| `max_queue` | 5 | 等待队列容量，不含正在处理的任务，0～100 |
| `cooldown_seconds` | 60 | 同一平台实例、同一用户跨会话共享冷却秒数 |
| `download_timeout_seconds` | 600 | 下载超时秒数，不包含排队和打包 |
| `max_zip_mib` | 50 | 插件文件上限，不能扩大 QQ / NapCat 的接口限制 |
| `zip_password` | 空字符串 | 留空生成普通 ZIP，否则使用 AES-256 加密 |
| `retention_hours` | 24 | 过大或上传失败 ZIP 的保留小时数 |
| `onebot_remote_jobs_dir` | 空字符串 | 插件 jobs 目录在 NapCat 中的绝对路径；同机同路径可留空 |
| `proxy` | 空字符串 | 使用系统代理；也可指定代理 URL |
| `api_domains` | 空列表 | 使用下载库默认域名；可填不含协议、路径的 API 主机名 |

每人最多一个未完成任务；队列满时拒绝新任务，不消耗冷却额度。每个下载任务同时处理一个章节、四张图片。

密码由管理员统一设置，支持中文和空格，按输入原样使用。插件不在回复、日志、文件名和任务元数据中输出密码；AstrBot 保存的插件配置仍应由管理员保护。请通过自己的渠道将密码告知使用者。使用 **7-Zip、WinRAR 等支持 AES ZIP 的软件**解压；图片内容加密，目录和文件名仍可列出。加密失败会返回打包失败，不会退回无密码 ZIP。

章节至少四位、页码至少六位，超过时按整本最大位数统一补零；顺序来自下载库的章节及图片索引，不依赖原始文件名或完成时间。原扩展名与内容保留；已有 ZIP 不受新配置影响。

## 文件、故障与卸载

每个任务使用独立目录：

```text
data/plugin_data/astrbot_plugin_jm_download/jobs/job-<任务ID>/JM<编号>.zip
```

成功上传后清理任务文件；超出大小限制或发送失败时保留完整 ZIP，由管理员本机获取。过期清理每十分钟检查一次；重启不恢复队列，按标记清理已中断的自有任务目录。取消或重载会等待底层写盘和打包线程结束，再清理目录。

NapCat 通过 `upload_group_file` / `upload_private_file` 上传，使用受理时保存的平台实例和群号 / 用户号，不从独立会话字符串推测文件接收者。路径不可访问、权限不足和接口失败会作为发送失败保留 ZIP。检查双方共享卷、NapCat 日志及群文件权限。

QQ 官方回传通过 AstrBot 原生文件组件完成；接口返回不能证明客户端已下载。适配器内部未抛出的错误无法由插件确认。网络、编号不存在、部分下载失败、超时、打包失败均有独立提示。

插件的卸载收尾不会主动删除配置和插件数据；WebUI 或外部清理工具若另行选择删除数据，不由插件控制。卸载前先备份所需保留文件。清理仅作用于带插件所有权标记的任务目录。

## 开发与验证

在 Python 3.12 的独立虚拟环境中：

```text
python -m pip install -r requirements-ci.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest -q
python scripts/build.py
python scripts/smoke_install.py
```

CI 强制要求真实 SDK 可导入，不会因缺少 AstrBot 静默跳过适配器测试。测试使用合成图片和模拟 OneBot 接口，不请求真实作品。覆盖顺序、中文密码、完整性、限流、取消收尾、保留清理、多实例路由和路径映射。源码和 ZIP 均检查真实 SDK 初始化、停止；GitHub Actions 验证 Windows、Linux 和 Python 3.12 容器。打包从元数据读取版本，检查内容白名单、16 MiB 上限及 SHA-256。推送版本标签由验证通过的 Actions 发布 Release。

真实 QQ 收发仍需单独验收，步骤见 [兼容性记录](docs/COMPATIBILITY.md)。反馈请使用 [Issues](https://github.com/listen-hua/astrbot_plugin_jm_download/issues)，提供脱敏日志，勿上传凭据、密码或作品文件。

## 使用范围与相关项目

JMComic 为 JM 站点下载库，可能访问成人内容。请仅处理有权访问和分发的内容，并遵守使用平台的内容规则；不得向未成年人提供成人内容。[AstrBot EULA](https://github.com/AstrBotDevs/AstrBot/blob/master/EULA.md) 对色情等用途有限制，公开仓库不代表获准进入官方市场。市场提交资料见 [MARKETPLACE.md](docs/MARKETPLACE.md)，收录以官方审核为准。

感谢 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)、[AstrBot](https://github.com/AstrBotDevs/AstrBot)、[pyzipper](https://github.com/danifus/pyzipper) 和 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)，详见 [依赖致谢](THIRD_PARTY_NOTICES.md)。
