# 星轨档案

面向《崩坏：星穹铁道》任务剧情的本地与自托管检索工具。通过同一套网页界面查找台词、人物和任务，查看剧情上下文、首次开放版本与玩家回答对应的对白。

## 许可证

Copyright (C) 2026 JahsehXL。

项目原创程序代码采用 GNU Affero General Public License 第 3 版，SPDX 标识为 `AGPL-3.0-only`。完整条款见 [LICENSE](LICENSE)。程序按现状提供，适用协议中的无担保与责任限制条款。

分发程序时须遵守相应源码提供义务。修改程序并通过网络供用户交互时，应向这些用户提供该修改版本对应源码的获取方式。网页提供当前发行版的源码入口。部署修改版时，请同步更新源码入口，指向自己实际运行版本的完整对应源码。

第三方任务版本元数据、游戏文本、字体与运行依赖的授权信息见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 搜索与阅读

- **关键词**搜索原文、说话人、任务名与出处，支持少量角色别名。默认关键词搜索关闭错字匹配。
- **情节描述**使用文字候选与嵌入模型的语义候选混合检索，需要先配置 API 并建立索引。语义索引不可用时，页面会明确提示当前使用文字搜索。
- **近似文字**提供独立的错字匹配入口，适用于记不清原句的情况。
- 可按世界、任务开放版本、任务类型和说话人筛选。结果标明匹配来源，点击后定位到具体台词。
- 玩家回答及其后续对白按选择关系组织，其他条件剧情可以展开阅读。

阅读区默认展示命中段落。玩家回答合并为选择卡片，点击“切换回答”查看对应后续。没有选定回答时可主动选择，其他段落通过“其他剧情”中的列表访问。复制当前对白只包含当前展示的路径。

## 数据准备

仓库提供程序源码和带出处的任务版本元数据。完整剧情数据库由使用者导入并保存在部署目录中。

首次启动后，在管理页面导入原始数据目录、ZIP，或本工具导出的预处理数据包。原始目录应直接包含以下文件夹。

```text
TurnBasedGameData/
├── Config/
├── ExcelOutput/
├── Story/
└── TextMap/
    └── TextMapCHS.json
```

管理页面也支持从配置的社区数据仓库下载快照。默认数据源是 `DimbreathBot/TurnBasedGameData`，可在更新设置中修改。[社区数据仓库](https://github.com/DimbreathBot/TurnBasedGameData)

导入器解析任务脚本中的对白、玩家回答和条件分支，并建立 SQLite 全文索引。未关联任务脚本的散落文本会跳过。已识别指令覆盖仍有限，版本未知、文本缺失和回顾路径未核实等状态会在界面中显示。

## Windows 本地运行

使用已打包版本时，解压整个发行文件夹并双击 `StorySearch.exe`，浏览器会自动打开。关闭启动程序窗口即可停止服务。数据库和配置保存在程序旁的 `data` 文件夹，也可通过 `--data-dir` 指定其他目录。

从源码运行需要 Python 3.12 和 Node.js 24。在项目目录执行以下命令。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
npm --prefix frontend ci
npm --prefix frontend run build
.\.venv\Scripts\python.exe launcher.py
```

文字搜索可以离线使用。情节描述检索需要调用配置的嵌入 API。

## Docker 自托管

需要已安装 Docker 与 Docker Compose 的 Linux 主机。项目使用同一 Dockerfile 支持 ARM64 与 AMD64，镜像在目标主机上原生构建。

在项目目录建立 `.env`，设置独立的阅读访问码与管理员凭据。

```dotenv
HSR_ACCESS_CODE=replace-with-a-random-reader-code
HSR_ADMIN_CODE=replace-with-a-different-random-admin-code
HSR_PUBLIC_PREFIX=/
```

访问码至少 8 字符，管理员凭据至少 12 字符。为容器的运行用户设置持久化目录写入权限，然后启动服务。

```sh
mkdir -p data
sudo chown 10001:10001 data
chmod 600 .env
docker compose up -d --build
```

Compose 将服务绑定到服务器回环地址 `127.0.0.1:8765`。在自己的 HTTPS 站点中配置反向代理，例如将下面的 `location` 放进已有 Nginx HTTPS `server` 块。

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 300;
    proxy_request_buffering off;
    client_max_body_size 16m;
}
```

将 `.env` 中的 `FORWARDED_ALLOW_IPS` 设置为反向代理连接容器时使用的实际来源 IP。宿主机通过 Docker 端口转发访问时，该地址通常是对应 Docker 网络的网关。应用据此识别 HTTPS 并设置安全会话 Cookie。

如果挂载在 `/tools/hsr/` 等子路径，将 `HSR_PUBLIC_PREFIX` 设为该路径，并让反向代理去掉路径前缀后转发到应用。路径需以 `/` 开始和结束。

浏览器打开配置好的 HTTPS 地址，使用管理员凭据登录后上传数据 ZIP 或下载社区数据快照。容器中可访问的原始目录也可直接导入。剧情数据库、索引、设置和任务记录持久保存在 `data` 中。

## 嵌入 API 与索引

接口需要兼容 JSON `POST /embeddings` 请求，并返回 `data[].embedding`。管理页面可配置 API 基础地址、模型、密钥、维度、查询前缀、文档前缀及额外参数。

保存设置后，先测试连接，再建立语义索引。建索引会把剧情片段发送给所配置的 API，搜索时会发送查询文本，费用按该服务计收。

查询与剧情索引必须使用兼容的模型和处理配置。更换模型、维度或前缀后需要对应的新索引。更新剧情时，相同模型和内容的已有向量缓存可以复用。

不同嵌入模型的语义结果和排序可能不同。共用一个服务器的用户使用同一套配置，无须各自计算。独立本地安装若需复现语义结果，应使用相同数据快照、模型版本、索引和检索配置。

API 密钥由管理员在各自部署中配置。设置与诊断接口只返回密钥是否已配置，导出数据包只包含剧情、元数据及已有索引。

## 更新、版本元数据与备份

管理页面支持导入原始目录或 ZIP、下载社区仓库快照，以及导入和导出预处理数据包。新数据校验成功后切换活动快照，上一快照可用于恢复。索引任务提供进度与暂停、继续、取消控制。

网页上传使用 8 MiB 分块，短暂中断时会重试并查询已接收的位置。更新清单和数据包下载使用 HTTPS。暂存文件及历史快照保存在 `data` 中，备份时保留该目录。收藏保存在当前浏览器，可以在收藏页面导出。

`backend/resources/mission_versions.json` 内置逐任务核对的开放版本，每条包含来源页面、检索日期和核实方法。导入与安装快照时，程序校验任务 ID、名称、章节、世界和类型，再补充空缺版本。管理员的手动标注会保留。尚无依据的版本显示“版本待核实”。

版本记录采用 BWIKI 任务页面明确标注的所属版本，文件保留 BWIKI 贡献者署名与 CC BY-NC-SA 4.0 标记。每条记录都链接到相应任务页面。[BWIKI](https://wiki.biligame.com/sr/)

同步元数据的命令如下。`--database` 指向已有剧情数据库。`--offline` 使用本机研究缓存，在线刷新需要能访问 BWIKI 的网络，也可用 `--proxy` 指定代理。

```sh
python scripts/sync_versions.py --database /path/to/story.db
python scripts/sync_versions.py --database /path/to/story.db --offline
```

原始剧情和游戏素材的权利归相应权利人所有。数据获取与分发由部署者按所用数据源和实际用途确认。

## 开发与打包

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
npm --prefix frontend test
npm --prefix frontend run build
.\.venv\Scripts\pyinstaller.exe StorySearch.spec
```

Windows 发行目录位于 `dist/StorySearch`，包含 Python 运行时、SQLite 扩展和前端文件。可通过管理页面向该发行目录导入数据，或另行提供预处理数据包。

后端使用 FastAPI、SQLite FTS5 与 sqlite-vec，前端使用 React、TypeScript 与 Vite。文字检索最多召回 500 个候选，情节描述检索再加入最多 500 个语义候选并按排名融合。候选达到上限时，页面提示“优先展示”，可通过筛选缩小范围。

界面附带 Archivo 拉丁字体子集，使用 SIL Open Font License，许可证位于 `frontend/src/assets/fonts/OFL-Archivo.txt`。中文使用设备上的中文字体，运行界面无需从外部字体服务下载资源。
