# 星轨档案 · 小红书小工具

小红书小工具与网页版同属星轨档案，复用剧情数据、阅读器、分支显示和基础样式。小工具从包内数据运行精准关键词检索，支持台词、任务、角色别名、四项组合筛选、开拓航线、原句定位、多出处、本机收藏和深浅色主题。描述找剧情、错字容错和数据管理通过网页版提供。 [小工具源码](../xiaohongshu-minitool/)

## 下载上传包

在小红书创作服务平台进入 Builder Hub → 小工具，在部署页第二步上传 ZIP。创服平台模拟器和扫码预览使用同一份上传包。 [下载 1.0.0 ZIP](https://github.com/JahsehXL/hsr-story-search/releases/download/minitool-v1.0.0/xinggui-dangan-xiaohongshu.zip) · [创作服务平台](https://creator.xiaohongshu.com/)

1.0.0 对应游戏剧情 4.7、本地数据快照 `505ddbc5151e4fdb8a634822`，包含 210,901 条台词与选项、2,042 个可检索任务。ZIP 为 9,417,371 字节，SHA-256 为 `d044064e340342fed06f040a92ae0d716bc20bbe98563ba2304649dccdfa73cb`。该包已在创服模拟器验证关键词检索、原句与多出处、分支切换，以及顶栏与宿主胶囊同排时的收藏和主题按钮。Android 和 iOS 真机仍待验证。

## 从源码构建

在仓库根目录运行以下 PowerShell 命令。已有项目环境可沿用现有 `.venv` 和前端依赖。

    py -3.12 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    npm --prefix frontend ci

先在本地星轨档案的「管理 → 数据与更新」导入一个标准剧情数据包，或用数据维护流程从原始游戏数据生成候选数据库。构建默认读取 `data/current.json` 指向的快照。若使用独立构建的数据库，可把它放在 `data/snapshots/<快照ID>/story.db`，并给构建与验证命令都传入同一个 `--snapshot <快照ID>`。检索和打包只使用剧情数据库。 [数据维护指南](data-maintenance.md) · [项目构建指南](build-guide.md)

    .\.venv\Scripts\python.exe -X utf8 .\xiaohongshu-minitool\build.py
    .\.venv\Scripts\python.exe -X utf8 .\xiaohongshu-minitool\verify.py .\xiaohongshu-minitool\build\corpus

默认产物为 `xiaohongshu-minitool/releases/xinggui-dangan-xiaohongshu.zip`，可以用 `--output` 指定其他路径。构建输出面向 ES2017 和 Chrome 61 的静态包，并检查入口、包内引用、CRC 和 10 MiB 上限。压缩包的 SHA-256 可用以下命令核对。

    Get-FileHash .\xiaohongshu-minitool\releases\xinggui-dangan-xiaohongshu.zip -Algorithm SHA256

## 同步数据与验证

1. 按数据维护流程核验新快照，再运行上述构建命令。导出器沿用后端任务顺序和镜像脚本映射，将索引与语料拆成独立文件。首屏加载小型目录，搜索或打开收藏时按需读取包内数据。
2. 用验证命令对照网页的候选结果、顺序、命中原句、版本、多出处和上下文。更新任务归属时，加上 `--previous <旧快照ID>`，验证旧收藏仍能凭稳定台词 ID 找到当前任务。
3. 上传前按创服部署页提供的最新版改写口令核对包。将口令指定的技能解压到 `xiaohongshu-minitool/.codex/` 后，构建会在技能可用时执行其 ZIP 审计。小工具读取宿主原生按钮的位置，为顶栏预留横向空间。 [小红书容器文档](https://miniapp-sandbox.xiaohongshu.com/minitool/doc#s3-11)
4. 使用相同的 ZIP 依次测试创服模拟器、Android 和 iOS。核对首页胶囊避让、冷启动首次搜索、长任务滚动、分支切换、返回结果和重开后收藏恢复。

源码与说明随主仓库维护。构建目录、私人数据库、平台技能副本与本地 ZIP 通过 Git 忽略规则管理。供下载的 ZIP 通过同一仓库的 Releases 分发。 [小红书版发布记录](https://github.com/JahsehXL/hsr-story-search/releases/tag/minitool-v1.0.0)
