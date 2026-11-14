# 星轨档案 · StarRail Story Archive

原始剧情数据来自 [TurnBasedGameData](https://github.com/DimbreathBot/TurnBasedGameData)，主要依赖 [React](https://github.com/react/react)、[FastAPI](https://github.com/fastapi/fastapi)、[sqlite-vec](https://github.com/asg017/sqlite-vec) 和 [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz)，语义索引使用 [BGE-M3](https://huggingface.co/BAAI/bge-m3)。

《崩坏：星穹铁道》剧情检索与阅读工具。按台词、角色或情节描述查找任务，查看前后文、玩家对话分支和任务首次开放版本。

**推荐使用在线版，打开即可检索。**

[进入星轨档案](https://hsr.jahsehxx.dpdns.org) · [从个人博客进入](https://jahsehxx.dpdns.org/tools/hsr/)

## 在线使用

- **精准关键词**默认可用，支持原句、忽略标点、空格组合搜索和常见角色外号，例如“老杨”“卡妈”。
- **错字容错**适合台词记忆不完整的情况，查询至少输入 3 个字。
- **描述找剧情**融合 `BAAI/bge-m3` 语义向量和人物、地点、主题线索，适合只记得大致情节时使用，需要配置自己的语义密钥。
- 可以组合筛选世界、首次开放版本、任务类型和角色。打开结果后定位命中台词，阅读上下文并切换玩家选项对应的后续对白。

使用描述搜索时，点击“🔑 语义密钥”，填写支持 `BAAI/bge-m3` 的嵌入服务 API Key。例如可以使用硅基流动，也可以填写兼容 OpenAI `/embeddings` 接口的自定义服务地址。模型与参数需要匹配站点的预计算索引，服务费用和额度以对应提供商说明为准。[硅基流动](https://cloud.siliconflow.cn)

语义密钥保存在当前浏览器的 `localStorage`。查询优先由浏览器直连嵌入服务，直连受阻时由本站服务端在内存中单次代发。已有完整预计算索引时，使用者只需计算查询向量。

## Windows 本地版

**不再主动维护 Windows 便携版，不推荐使用历史便携包，建议使用在线版。** 历史包可能缺少后续的程序修复和游戏剧情数据，已有发布文件保留供需要者参考。[历史发布与数据包](https://github.com/JahsehXL/hsr-story-search/releases)

### 复制提示词，扔给 AI 直接部署

需要离线使用或自行构建 Windows 版本时，把下面的提示词交给能够在你电脑上执行命令的 AI，并提供项目目录及已有的数据包。

```text
请帮我在这台 Windows 电脑上从源码构建并运行“星轨档案”。
仓库地址为 https://github.com/JahsehXL/hsr-story-search 。
请先阅读仓库中的 docs/windows-build.md，按指南检查环境、安装依赖、
构建网页、使用 StorySearch.spec 打包，并完成程序自检和本地运行验证。
使用独立构建目录，保留已有文件与数据。
如果我提供了 hsr-data-*.zip，请校验并将其中的剧情和预计算索引加入便携目录。
缺少数据时说明可用的数据来源与覆盖版本，询问我需要使用哪一份数据。
完成后给出 ZIP 的绝对路径、SHA-256、源码版本、数据版本与实际验证结果。
```

环境要求、完整命令、可选的数据打包步骤和故障排查见构建指南。[Windows AI 构建与本地部署指南](docs/windows-build.md)

## 从源码运行

开发环境使用 Python 3.12 与 Node.js 24。以下 PowerShell 命令在项目根目录执行。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
npm --prefix frontend ci
npm --prefix frontend run build
.\.venv\Scripts\python.exe launcher.py
```

启动器会在回环地址启动服务并打开浏览器。首次启动的空实例需要导入剧情数据。

## Docker 自托管

支持 Linux `ARM64` 与 `AMD64` 主机原生构建。创建 `.env`，配置访问与管理凭据。`HSR_ACCESS_CODE` 留空时允许公开只读检索。

```dotenv
HSR_ACCESS_CODE=
HSR_ADMIN_CODE=replace-with-a-different-random-admin-code
HSR_PUBLIC_PREFIX=/
```

创建持久化目录并启动。`compose.yaml` 默认监听 `127.0.0.1:8765`，限制内存 `3g` 和 CPU `1.5`。

```sh
mkdir -p data
sudo chown 10001:10001 data
chmod 600 .env
docker compose up -d --build
```

通过 Nginx 提供 HTTPS 反向代理。挂载在 `/tools/hsr/` 等子路径时，把 `HSR_PUBLIC_PREFIX` 改为 `/tools/hsr/`，并将该子路径转发至 `http://127.0.0.1:8765/`。

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

## 数据导入与维护

本地运行时，可在“管理 → 数据与更新”导入剧情数据。

- **预处理数据包**使用 `hsr-data-*.zip`，其中可包含剧情数据库、版本标注与预计算向量索引。覆盖版本以对应发布说明为准。[数据包下载](https://github.com/JahsehXL/hsr-story-search/releases)
- **原始游戏数据**默认来源为社区仓库，可导入目录或 ZIP，也可以按管理流程从仓库拉取固定提交。[TurnBasedGameData](https://github.com/DimbreathBot/TurnBasedGameData)
- **任务首次开放版本**由逐任务核验的元数据提供，主要依据 BWIKI 任务页面，遵循 CC BY-NC-SA 4.0 署名要求。游戏数据版本、程序版本与任务首次版本分别记录。[崩坏：星穹铁道 BWIKI](https://wiki.biligame.com/sr/)

原始目录应包含以下文件结构。

```text
TurnBasedGameData/
├── Config/
├── ExcelOutput/
├── Story/
└── TextMap/
    └── TextMapCHS.json（或 TextMapMainCHS.json）
```

后续游戏更新由维护者或其 AI 按文档检查来源提交、构建候选数据、核验差异并验证检索结果。导入诊断、已知修复原因和版本记录方法见维护流程。[数据导入、修复与 AI 维护指南](docs/data-maintenance.md)

任务版本元数据保存在 `backend/resources/mission_versions.json`。可用以下命令同步或从本地缓存刷新，完成后按维护指南核验结果。

```sh
python scripts/sync_versions.py --database /path/to/story.db
python scripts/sync_versions.py --database /path/to/story.db --offline
```

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
npm --prefix frontend test
npm --prefix frontend run build
```

Windows 打包和产物验证的命令见专门指南。[Windows 构建指南](docs/windows-build.md)

## 开源许可与声明

Copyright (C) 2026 JahsehXL。

- 本项目原创程序代码采用 **GNU Affero General Public License v3.0 (`AGPL-3.0-only`)** 授权，完整条款见许可文件。程序按“现状”提供，适用协议中的无担保与责任限制条款。[LICENSE](LICENSE)
- 第三方任务版本元数据、游戏文本、字体（Archivo，SIL Open Font License）与运行依赖的授权说明见第三方声明。原始剧情文本与游戏相关素材的著作权归米哈游（miHoYo / HoYoverse）及相关权利人所有。[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
