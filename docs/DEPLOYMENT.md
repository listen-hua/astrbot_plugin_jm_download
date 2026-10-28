# 部署与维护

## Windows / Linux

准备 Python 3.12 和 AstrBot ≥4.29.3、<5。优先在 WebUI 使用仓库链接安装或 Release ZIP 安装，依赖由 AstrBot 安装。网络需要能连接 Python 包索引及下载站点；插件不接管机器人连接、账号登录或密钥。

QQ 官方连接参考 [WebSocket 文档](https://docs.astrbot.app/platform/qqofficial/websockets.html)。NapCat 请按其当前文档建立 OneBot V11 连接，在 AstrBot 选择对应的 `aiocqhttp` 平台并确认在线。群文件需要对应群的文件上传权限。

如果两个进程运行在同一台机器且能访问同一绝对路径，`onebot_remote_jobs_dir` 留空。NapCat 用户必须有读取插件 jobs 目录及 ZIP 的权限。Linux 检查目录遍历权限；Windows 检查进程账号和共享权限。单独部署的机器没有共享文件系统时，仅填写映射路径不会传输文件。

### Windows 启动器脚本

在下载的源码仓库中显式指定目标实例 core 路径：

```powershell
python scripts/install.py --core-path "C:/AstrBot Instances/my-bot/core"
```

若运行数据根目录独立于 core，加 `--root "C:/AstrBot Data/my-bot"`。路径可以包含空格。脚本要求目标 `core` 具有 AstrBot 源码，并使用相邻 `venv/Scripts/python.exe`；没有默认实例或自动搜索目标。

脚本安装依赖到目标运行目录的 `data/site-packages`，约束已有依赖版本，冲突时明确失败而不强行升级核心。重复安装前备份当前插件源码到 `data/backups/jm-plugin`。不会删除配置及插件数据；完成后在 WebUI 重载或重启该实例。`--skip-deps` 仅用于已经具备固定依赖的环境。

## Docker 共享目录示例

为已经运行的 AstrBot 与 NapCat 服务增加以下卷配置（合并到自己的 Compose；不是包含登录及连接配置的完整部署文件）：

```yaml
services:
  astrbot:
    volumes:
      - ./astrbot-data:/AstrBot/data
      - jm-download-jobs:/AstrBot/data/plugin_data/astrbot_plugin_jm_download/jobs
  napcat:
    volumes:
      - jm-download-jobs:/jm-downloads:ro
volumes:
  jm-download-jobs:
```

以上示例假设 AstrBot 的运行根目录是 `/AstrBot`。如设置 `ASTRBOT_ROOT`，对应调整左侧容器中的挂载位置，以实际 `data/plugin_data/astrbot_plugin_jm_download/jobs` 为准。NapCat 服务名、其他卷、网络、镜像和登录配置沿用自己的部署。

将插件配置 `onebot_remote_jobs_dir` 设为：

```text
/jm-downloads
```

映射示例：AstrBot 中的 `.../jobs/job-abc/JM123.zip` 对应 NapCat 中的 `/jm-downloads/job-abc/JM123.zip`。映射根必须对应 **jobs 目录本身**，不能填到其父目录或某个单独任务目录。只读挂载足够用于读取和上传；AstrBot 挂载必须可写。

插件只映射已存在、解析后仍在自身 jobs 目录中的 ZIP，拒绝越界及包含 `..` 的映射根。映射不会检查远端实际挂载状态；错误挂载或权限问题会由 NapCat 上传失败返回。请用管理员自己的合成文件先验证 NapCat 进程能读取相应路径。

共享卷不要用于不相关服务；保留文件和解压密码配置都属于管理员数据。若两个容器以不同 UID 运行，需要在部署层设置双方可读权限。安装插件或修改挂载后重建相关服务，再重载插件。

## 升级、重载与卸载

1. 备份 AstrBot 的 `data`，包括插件配置和所需保留 ZIP。
2. 用 WebUI 更新仓库，或安装新 Release ZIP；手动脚本会另行备份源码。
3. 保持已有配置值，新键使用默认值。保存并重载后检查版本及配置。
4. 重载会取消排队任务、等待正在写盘的工作释放资源；重启不恢复任务。

插件的 `terminate()` 只停止工作，不删除配置或保留 ZIP。程序启动和定时清理遵循任务标记及保留期。卸载时若管理界面提供“删除数据”选项，请先备份；AstrBot 或第三方清理动作的行为以其提示为准。

## 本地构建

```text
python -m pip install -r requirements-ci.txt
python scripts/build.py
python scripts/smoke_install.py
```

安装包位于 `dist`，版本来自元数据。`SHA256SUMS` 包含安装包的 SHA-256，可用 PowerShell `Get-FileHash -Algorithm SHA256` 或 Linux `sha256sum` 校验。构建脚本验证文件白名单、内容一致性及 16 MiB 大小上限，包中不含测试图片、依赖、运行数据或私人配置。
