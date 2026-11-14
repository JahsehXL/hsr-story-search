# 构建与部署指南（供使用者与 AI 执行）

本文汇总源码运行、Windows 打包、嵌入模型配置与剧情向量索引构建流程。使用现有 BGE-M3 数据包时可直接准备数据，需要其他模型时按“模型配置与剧情索引构建”一节生成自己的数据包。

项目不再主动维护 Windows 便携版，不推荐使用历史便携包，建议使用在线版。确有离线需求时，可以按本文从源码自行构建并验证自己的 Windows 版本。[在线版](https://hsr.jahsehxx.dpdns.org)

## 复制提示词，扔给 AI 直接部署

```text
请帮我在这台 Windows 电脑上从源码构建并运行“星轨档案”。
仓库地址为 https://github.com/JahsehXL/hsr-story-search 。
请先阅读仓库中的 docs/build-guide.md，按指南检查环境、安装依赖、
构建网页、使用 StorySearch.spec 打包，并完成程序自检和本地运行验证。
使用独立构建目录，保留已有文件与数据。
如果我提供了 hsr-data-*.zip，请校验并将其中的剧情和预计算索引加入便携目录。
如果我指定其他嵌入模型，请按本指南配置服务、重建并验证完整索引，
导出标准数据包后再加入便携目录，同时记录模型参数与调用额度。
缺少数据时说明可用的数据来源与覆盖版本，询问我需要使用哪一份数据。
完成后给出 ZIP 的绝对路径、SHA-256、源码版本、数据版本与实际验证结果。
```

执行此任务的 AI 需要能在本机运行命令。用户已有工作副本时，优先读取该目录的项目说明与当前构建配置。

本次构建的交付物是完整的 `StorySearch` 文件夹及其 ZIP。使用者解压后双击 `StorySearch.exe`，程序会启动本地服务并打开浏览器。

## 环境与源码

- 使用 Windows x64、Python 3.12 x64、Node.js 24 和 npm。Git 用于获取源码与记录提交。
- 安装依赖、下载源码和数据包需要网络。已有剧情与索引后，关键词检索可以离线使用。描述搜索还需要调用兼容模型的嵌入服务。
- PyInstaller 的产物取决于构建时的操作系统和 Python 架构，Windows x64 包应在 Windows x64 的 Python 上构建。参见 [PyInstaller 官方文档](https://pyinstaller.org/en/stable/operating-mode.html)。
- 预留依赖安装、数据包、解压数据库及验证副本所需的磁盘空间。

首次获取源码可执行以下命令。已有工作副本时，进入其根目录并保留其中的修改。

```powershell
git clone https://github.com/JahsehXL/hsr-story-search.git
Set-Location hsr-story-search
```

后续命令都在仓库根目录执行。各节 PowerShell 命令在同一会话执行，逐条检查外部程序的 `$LASTEXITCODE`，失败时先解决错误再进入下一步。

```powershell
git status --short
git rev-parse HEAD
py -3.12 --version
node --version
npm --version
```

获取源码 ZIP 的使用者可以跳过 Git 命令，并在交付说明中记录下载地址和版本。工具缺失时先完成对应工具的安装，再继续。

## 安装构建依赖

首次创建虚拟环境。已有 `.venv` 时先检查其 Python 版本和架构，选择可用的 Python 3.12 x64 环境。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -c "import sys, struct; print(sys.version); print('bits:', struct.calcsize('P') * 8); assert sys.version_info[:2] == (3, 12); assert struct.calcsize('P') == 8"
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m PyInstaller --version
npm --prefix frontend ci
```

使用仓库的 `requirements-dev.txt` 和 `frontend/package-lock.json` 安装依赖。记录实际安装的 PyInstaller 版本，当前开发依赖允许一定版本范围，构建产物可能随工具版本变化。

## 构建网页和程序

```powershell
npm --prefix frontend run build
```

确认 `frontend/dist/index.html` 已生成，再创建独立输出目录。

```powershell
if (!(Test-Path -LiteralPath frontend/dist/index.html)) { throw '缺少网页构建产物' }
$BuildId = [guid]::NewGuid().ToString('N')
$BuildRoot = Join-Path (Get-Location).Path "build/windows-$BuildId"
New-Item -ItemType Directory -Path $BuildRoot -ErrorAction Stop | Out-Null
$DistRoot = Join-Path $BuildRoot 'dist'
$WorkRoot = Join-Path $BuildRoot 'pyinstaller'
.\.venv\Scripts\python.exe -m PyInstaller StorySearch.spec --distpath "$DistRoot" --workpath "$WorkRoot"
```

成功后设置产物路径，并检查可执行文件。

```powershell
$Bundle = Join-Path $DistRoot 'StorySearch'
$Executable = Join-Path $Bundle 'StorySearch.exe'
if (!(Test-Path -LiteralPath $Executable)) { throw '缺少 StorySearch.exe' }
```

`StorySearch.spec` 会收集前端资源、任务版本元数据、SQLite 向量扩展与许可文件。整个 `StorySearch` 目录需要一起交付，包括 `_internal` 子目录。

## 模型配置与剧情索引构建

在线版与现有预计算数据包采用 `BAAI/bge-m3`，原生 1024 维，查询和文档前缀均为空。本站使用硅基流动生成该索引，服务商仅为配置示例，不构成推广。使用者可以选择提供相同模型、匹配参数和兼容接口的服务。

### BGE-M3 与免费服务

截至 2026-11-14，两个平台的官方说明如下。免费服务的额度、限速和价格随平台政策调整。

| 平台 | 模型标识 | 免费使用方式 |
| --- | --- | --- |
| 硅基流动 | `BAAI/bge-m3` | 标准版当前标价免费，另有单独计费的 `Pro/BAAI/bge-m3` |
| Cloudflare Workers AI | `@cf/baai/bge-m3` | 可使用 Workers AI 每日 10,000 Neurons 的免费额度，额度由账户内 Workers AI 调用共享 |

来源为平台官方价格与模型页面。[硅基流动价格](https://www.siliconflow.cn/pricing) · [Cloudflare BGE-M3](https://developers.cloudflare.com/workers-ai/models/bge-m3/) · [Cloudflare 额度与计费](https://developers.cloudflare.com/workers-ai/platform/pricing/)

### 使用现有索引

1. 在“语义密钥”中填写自己的 API Key。默认请求硅基流动的 `https://api.siliconflow.cn/v1/embeddings`。
2. 使用其他兼容服务时展开“其他兼容平台接口地址”，填写其 `/embeddings` 地址或基础地址。程序会按当前索引记录的模型、查询前缀、维度和额外参数发送查询请求。
3. 核对服务确实提供相同的 BGE-M3 模型和 1024 维输出，再用已知台词、人物和场景抽查描述检索结果。跨平台输出与预处理的一致性仍需实际核验。

默认硅基流动连接优先从浏览器直连，失败时由本站服务端在内存中代发。自定义接口当前使用浏览器直连，需要正确配置 CORS，允许页面来源以及 `Authorization`、`Content-Type` 请求头。密钥保存在当前浏览器中。

#### Cloudflare 接入要点

Cloudflare 提供的 OpenAI 兼容嵌入接口为以下地址，其中账户 ID 使用自己的值。

```text
https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1/embeddings
```

Cloudflare 的模型名称为 `@cf/baai/bge-m3`，现有数据包中记录的名称为 `BAAI/bge-m3`。接入现有索引时，兼容转发服务需要将请求里的 `BAAI/bge-m3` 映射到 Cloudflare 对应模型，并返回 OpenAI 兼容的 `data[].embedding` 数组。浏览器直连还需要服务支持相应 CORS。

可以让自己的 AI 按 Cloudflare 文档配置兼容网关，处理模型名映射和 CORS。确认接口格式、输出维度和查询结果后，再在“其他兼容平台接口地址”填写该网关地址。[Cloudflare OpenAI 兼容接口](https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/)

### 使用其他嵌入模型重建索引

更换模型时，需要为全库剧情片段计算新向量，并让查询采用相同模型与参数。两个模型即便维度相同，其向量也需要分别建立索引。项目按模型、维度、前缀和额外语义参数区分索引身份。

#### 1. 创建独立的本地实例

完成前面的依赖安装与网页构建后，在仓库根目录执行以下 PowerShell 命令。为实验使用独立目录，保留原有可用数据。

```powershell
$ModelData = Join-Path (Get-Location).Path ("build/model-lab-" + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe launcher.py --data-dir "$ModelData"
```

#### 2. 配置目标模型并导入剧情

在本地“管理 → 搜索 API”填写提供 OpenAI 兼容 `/embeddings` 的服务基础地址、API Key 和目标嵌入模型名称。按模型说明设置查询与文档前缀、额外参数、批次和并发。维度留空可自动检测输出维度（内部配置为 0），明确填写维度前应确认模型支持相应请求参数。

使用自建模型服务时，先确保接口能接收一组文本并按原顺序返回浮点向量。通过“保存并测试连接”检查凭据、模型与输出维度。

随后在“管理 → 数据与更新”导入原始剧情数据或已有 `hsr-data-*.zip`。原始数据导入在已配置密钥时可能直接开始构建索引，应先核对平台额度。导入已有 BGE-M3 数据包后，模型不匹配会显示索引不可用，需要执行下一步。

#### 3. 建立全库向量索引

在本地“数据与更新”中执行“建立 / 补全向量索引”。构建器使用当前配置，按内容哈希复用同一模型身份的缓存，仅计算缺失向量。换用其他模型时会重新计算相应内容，旧模型缓存与新模型索引分别保存。

全库构建的调用量远高于一次查询。免费额度与速率限制也适用于构建任务，可以降低并发和批次，按任务状态暂停或继续。完成后确认剧情片段与索引条数一致，索引模型和维度正确，并用多条已知剧情测试描述检索及上下文。

#### 4. 导出与分享

通过本地管理界面导出标准数据包。需要将自建索引附带到 Windows 便携版时，将导出的 ZIP 作为下一节的 `$DataPackage`，按相同校验流程加入新构建目录。记录模型名称、维度、前缀、额外语义参数、剧情来源版本及验证结果。接收者导入后，也需配置相同模型与参数，才能使用该索引进行查询。源文件或分段规则变化时，按数据维护流程重新生成候选、比较内容并补齐索引。[数据维护流程](data-maintenance.md)

#### 可交给 AI 的提示词

```text
请先阅读 docs/build-guide.md 和 docs/data-maintenance.md，
使用我提供的嵌入服务与模型，在独立数据目录构建星轨档案剧情索引。
先检查接口格式、模型名称、维度、查询和文档前缀及调用额度，
再导入指定剧情数据，计算该模型的完整向量索引。
完成后核对向量条数并抽查描述检索，导出标准数据包，
记录剧情来源、模型参数、实际验证和预计持续调用成本。
```

## 准备剧情与向量数据

默认构建完成后，程序首次运行时可在“管理 → 数据与更新”中安装数据包或导入原始数据。源码仓库中的 `data/` 被 Git 忽略，打包配置也没有将它自动加入产物。

需要附带数据时，优先使用 [Releases](https://github.com/JahsehXL/hsr-story-search/releases) 中的 `hsr-data-*.zip`。记录具体资源 URL、数据版本和 SHA-256。历史数据包覆盖的剧情版本以该次发布说明为准。

下面的可选步骤使用项目现有的 `Runtime.install_package` 校验清单、数据库大小、哈希和数据库结构，生成可携带的数据目录。它会保留数据包已有的向量索引。若发布方提供独立的校验值，还应比对下载文件的 SHA-256。

将路径替换为使用者实际提供或下载的数据包，然后执行。

```powershell
$DataPackage = (Resolve-Path -LiteralPath 'C:\Downloads\hsr-data-实际快照.zip').Path
Get-FileHash -LiteralPath $DataPackage -Algorithm SHA256
@'
from pathlib import Path
import shutil
import sys
import tempfile

from backend.runtime import Runtime

package = Path(sys.argv[1]).resolve()
bundle = Path(sys.argv[2]).resolve()
target = bundle / "data"
if not package.is_file():
    raise SystemExit("Data package does not exist")
if not (bundle / "StorySearch.exe").is_file():
    raise SystemExit("Portable executable does not exist")
if target.exists():
    raise SystemExit("Use a fresh build output with no existing data directory")

with tempfile.TemporaryDirectory(prefix="hsr-package-") as temporary:
    work = Path(temporary)
    runtime = Runtime(work / "data", "local")
    try:
        snapshot = runtime.install_package(package, work / "unpacked")
        print("Data:", runtime.status_data())
        print("Embedding:", runtime.embedding_status())
        target.mkdir()
        shutil.copy2(runtime.root / "current.json", target / "current.json")
        shutil.copytree(runtime.root / "snapshots" / snapshot,
                        target / "snapshots" / snapshot)
    finally:
        runtime.close()
print("Portable data:", target)
'@ | .\.venv\Scripts\python.exe - "$DataPackage" "$Bundle"
```

这里生成的数据只包含当前快照与指针。开发者日常使用的 `data/` 还可能含 API 密钥、设置、日志和历史任务，分发前应核对交付目录的文件。

使用原始数据的使用者可以先启动程序，在管理界面导入，再导出标准数据包供上述步骤使用。原始文本导入后，关键词检索即可使用。构建完整向量索引需要自己的嵌入服务密钥，会产生对应服务的调用量与可能的费用。导入已有预计算索引可省去这次全库计算。

原始数据的来源提交、版本记录、导入诊断和候选数据核验步骤见维护指南。[数据导入、修复与 AI 维护指南](data-maintenance.md)

## 自检、压缩与运行验证

```powershell
& $Executable --self-test
if ($LASTEXITCODE -ne 0) { throw '程序自检失败' }
```

自检成功会输出 `SELF_TEST_OK`，覆盖 SQLite FTS5、向量扩展、前端文件、版本元数据和后端加载。剧情内容与浏览器交互还需要下面的运行验证。

先压缩干净的构建目录，再从解压副本运行，验证过程中填写的个人设置会保存在该副本中。

```powershell
$Version = & .\.venv\Scripts\python.exe -c "from backend import __version__; print(__version__)"
$ZipPath = Join-Path $BuildRoot "StarrailStorySearch-Windows-x64-$Version.zip"
Compress-Archive -LiteralPath $Bundle -DestinationPath $ZipPath -ErrorAction Stop
Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256
$VerifyRoot = Join-Path $BuildRoot 'verify'
Expand-Archive -LiteralPath $ZipPath -DestinationPath $VerifyRoot -ErrorAction Stop
$VerifyExecutable = Join-Path $VerifyRoot 'StorySearch/StorySearch.exe'
& $VerifyExecutable --self-test
if ($LASTEXITCODE -ne 0) { throw '解压副本自检失败' }
& $VerifyExecutable
```

最后一条命令持续运行本地服务，验证结束后在该终端按 Ctrl+C 退出。检查以下行为。

1. 浏览器自动打开并进入本地界面，网页样式和字体正常。
2. 空数据包可打开管理界面并导入数据。附带数据的包可直接查看任务目录、搜索已知台词并打开上下文。
3. 若附带向量索引，检查描述搜索入口的可用状态。提供自己的语义密钥后，再验证一条描述查询，并记录是否实际完成该项。
4. 退出后重新启动，确认验证副本的数据仍能加载。

自动化环境无法查看浏览器时，记录已完成的自检和待使用者验证的交互步骤。验证机若设置了 `HSR_DATA_DIR`，应先检查它对数据目录选择的影响。

## 常见问题

| 现象 | 排查方式 |
| --- | --- |
| 找不到 Python 3.12 或 Node.js 24 | 检查安装版本和 PATH，重新打开终端后确认版本 |
| pip / npm 下载失败 | 检查网络和代理，保留错误输出，修复连接后重试依赖安装 |
| 网页文件缺失 | 确认前端构建成功且生成 `frontend/dist/index.html`，再重新打包 |
| 自检报 DLL 或 sqlite_vec 加载错误 | 检查 Python 为 x64，并核对 `StorySearch.spec` 收集的扩展二进制及 PyInstaller 构建日志 |
| 双击后立即退出 | 在 PowerShell 执行 `StorySearch.exe --self-test` 查看错误，确认整个目录及 `_internal` 完整 |
| 程序启动但没有剧情 | 检查数据导入结果，以及 `data/current.json` 指向的 `data/snapshots/<快照>/story.db` 是否存在 |
| 手动打开网址后提示本地会话失效 | 关闭程序并重新启动，让启动器自动打开带一次性登录令牌的页面 |
| 8765 端口被占用 | 启动器会尝试其他可用端口，使用这次启动自动打开的页面 |
| 关键词可用，描述搜索不可用 | 检查数据包是否含完整索引，以及查询服务的模型、参数和个人密钥是否匹配 |

## AI 的交付记录

交付时给出 ZIP 的绝对路径与 SHA-256，并记录源码提交或下载版本、构建工具版本、是否附带剧情与向量索引、所用数据包来源、自检结果及实际完成的运行验证。源码或依赖经过本地修复时，附上改动说明。让使用者能够区分已验证和待验证的功能。

分享构建产物时保留包内许可文件，并遵循 [LICENSE](../LICENSE) 与 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) 中的许可及第三方来源说明。
