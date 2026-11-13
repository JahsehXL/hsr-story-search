# ✦ 星轨档案 (StarRail Story Archive)

面向《崩坏：星穹铁道》任务剧情的本地与自托管检索工具。通过统一的剧本台词本界面查找台词、角色和任务，查看剧情前后文、首次开放版本与玩家分支选项对应的后续对白。

- 🌐 **在线网页版**：[https://hsr.jahsehxx.dpdns.org](https://hsr.jahsehxx.dpdns.org)（直达短域名） / [https://jahsehxx.dpdns.org/tools/hsr/](https://jahsehxx.dpdns.org/tools/hsr/)
- 📦 **Windows 便携包 & 预计算向量数据包**：[前往 GitHub Releases 下载](https://github.com/JahsehXL/hsr-story-search/releases)

---

## ✨ 功能特性

- **剧本台词本阅读体验**：采用左列角色、右列台词的台词本排版，打开结果自动平滑滚动并高亮定位命中原句，顶部标注任务类型（开拓 / 续闻 / 同行 / 冒险 / 日常）、所属世界与首次开放版本。
- **平铺直出玩家分支选项**：告别繁琐的下拉折叠菜单——所有玩家对话选项（`❖ 选项 1 / 2 / 3`）直接平铺展示，默认自动衔接第一项的后续分支对白，点击任意选项即可即时切换对应的专属回应。
- **三种搜索模式与多维筛选**：
  - **描述找剧情（推荐，需配置语义密钥）**：融合 `BAAI/bge-m3`（1024 维）语义向量与人物/地点/主题线索，适合只记得画面、人物或大致情节经过的自然语言搜索。
  - **精准关键词（默认，无需密钥）**：支持原句匹配、忽略标点连写、空格多词组合（跨角色名 + 台词联合检索，如 `三月七 星穹列车`、`老杨 休息`）以及常见角色外号映射（`老杨`、`小三月`、`饮月君`、`大黑塔`、`卡妈`、`萨姆`、`忘归人`、`鸟叔` 等）。
  - **错字容错（无需密钥）**：独立容错模糊匹配入口（至少 3 个字），适合记得大概台词但怕记混个别字或漏字时使用。
  - **多维筛选**：支持按**世界**、**首次开放版本**、**任务类型**和**角色**组合筛选，搜索框旁提供 `?`/`!` 快速说明弹窗。
- **一次建索引，全员零成本共享**：发布包与在线站点已内置 **`1.0` ~ `4.7` 共 2,866 个任务、180,363 条对白、100,496 个剧情片段**的 `BAAI/bge-m3` 预计算向量索引。访客或本地用户无需重新计算全库向量，只需配置自己的 API Key 即可秒级使用“描述找剧情”搜索。

---

## 🚀 快速上手

### 方式一：直接使用在线版 / Windows 便携包

1. **下载运行**：在 [Releases](https://github.com/JahsehXL/hsr-story-search/releases) 下载 `StarrailStorySearch-Windows-x64-*.zip`，解压后双击 `StorySearch.exe` 即可自动在浏览器打开（已内置完整剧情库与 `BAAI/bge-m3` 向量索引）；或直接访问[在线网页版](https://hsr.jahsehxx.dpdns.org)。
2. **开启“描述找剧情”语义搜索（推荐）**：
   - 前往 [硅基流动 SiliconFlow](https://cloud.siliconflow.cn) 按 **注册账号 → 实名认证 → 创建 API Key** 获取密钥（平台对 `BAAI/bge-m3` 模型提供免费额度；如已有其他兼容 OpenAI `/embeddings` 接口且使用 `BAAI/bge-m3` 的服务商，也可在界面中填自定义地址）。
   - 点击搜索栏下方的 **「🔑 语义密钥」** 按钮，粘贴你的 API Key 并保存，再切换到 **「描述找剧情」** 模式即可。
   - 密钥仅保存在你当前浏览器的 `localStorage` 中；搜索时优先由浏览器直连服务商计算单条查询向量，直连受阻时由服务端仅在内存中单次代发（不落盘、不扣减服务端配额）。

### 方式二：从源码本地运行

需要 Python 3.12 与 Node.js 24：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
npm --prefix frontend ci
npm --prefix frontend run build
.\.venv\Scripts\python.exe launcher.py
```

---

## 🐳 Docker 自托管部署

支持 Linux `ARM64` 与 `AMD64` 主机原生构建。

1. 在项目目录创建 `.env`（当 `HSR_ACCESS_CODE` 留空时默认开启**公开只读检索**，仅后台管理接口需要 `HSR_ADMIN_CODE`）：

```dotenv
HSR_ACCESS_CODE=
HSR_ADMIN_CODE=replace-with-a-different-random-admin-code
HSR_PUBLIC_PREFIX=/
```

2. 创建持久化数据目录并启动容器（`compose.yaml` 默认限制 `mem_limit: 3g`、`cpus: 1.5`，监听 `127.0.0.1:8765`）：

```sh
mkdir -p data
sudo chown 10001:10001 data
chmod 600 .env
docker compose up -d --build
```

3. 配置 Nginx HTTPS 反向代理（若挂载在 `/tools/hsr/` 等子路径，请将 `.env` 中的 `HSR_PUBLIC_PREFIX` 设为 `/tools/hsr/`，并在 Nginx 中将子路径转发至 `http://127.0.0.1:8765/`）：

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

---

## 📚 数据准备与向量索引

首次部署空实例时，可在右上角 **「⚙ 管理 → 数据与更新」** 中选择以下任一方式导入数据：

1. **安装预处理数据包（推荐）**：直接上传 Releases 中的 `hsr-data-*.zip`，一键导入完整剧情数据库、版本标注与预计算好的 `BAAI/bge-m3` 语义向量索引。
2. **从社区数据仓库在线拉取**：默认数据源为 [`DimbreathBot/TurnBasedGameData`](https://github.com/DimbreathBot/TurnBasedGameData)，支持配置 HTTPS GitHub 镜像前缀（兼容常见 `gh-proxy` 格式）。
3. **导入本地原始解包目录或 ZIP**：目录结构如下：

```text
TurnBasedGameData/
├── Config/
├── ExcelOutput/
├── Story/
└── TextMap/
    └── TextMapCHS.json
```

### 版本元数据核验

`backend/resources/mission_versions.json` 内置逐任务核对的首次开放版本，数据源自 [崩坏：星穹铁道 BWIKI](https://wiki.biligame.com/sr/) 任务页面（遵循 CC BY-NC-SA 4.0 署名要求）。可通过以下命令同步或离线刷新：

```sh
python scripts/sync_versions.py --database /path/to/story.db
python scripts/sync_versions.py --database /path/to/story.db --offline
```

---

## 🛠 开发测试与打包

```powershell
# 运行后端测试
.\.venv\Scripts\python.exe -m pytest tests -q

# 运行前端测试与构建
npm --prefix frontend test
npm --prefix frontend run build

# 打包 Windows 独立运行目录 (dist/StorySearch)
.\.venv\Scripts\pyinstaller.exe StorySearch.spec --noconfirm
```

---

## 📄 开源许可与声明

Copyright (C) 2026 JahsehXL。

- 本项目原创程序代码采用 **GNU Affero General Public License v3.0 (`AGPL-3.0-only`)** 授权，完整条款见 [LICENSE](LICENSE)。程序按“现状”提供，适用协议中的无担保与责任限制条款。
- 第三方任务版本元数据、游戏文本、字体（Archivo，SIL Open Font License）与运行依赖的授权说明详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。原始剧情文本与游戏相关素材的著作权归米哈游（miHoYo / HoYoverse）及相关权利人所有。
