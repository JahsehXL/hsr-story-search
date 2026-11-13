# 剧情数据维护与修复流程（供 AI 执行）

游戏更新后，由 AI 读取本流程、检查上游与现有快照，生成候选数据并验证，再按使用者授权部署。`scripts/data_tools.py` 提供独立的检查、重建、比较和元数据修复命令。各命令生成报告，保留现有数据库，便于判断与重试。

## 已修复过什么

原始数据中的任务脚本、台词表和演出资源分别描述不同部分，只遍历任务脚本会漏掉演出中的对白。此前的修复已写入正式解析器 `backend/data.py`，重新导入时会执行。

| 修复记录 | 原因与处理 |
| --- | --- |
| `e2d4223` | 原先只解析显式对话列表，遗漏 2.0–4.7 部分 timeline 台词。改为结合任务编号、现有台词引用和 Performance 路径恢复关联，并重新生成搜索内容 |
| `2868c2f` | 1.x 的编号规则不同，仍漏掉开场等旧剧情。通过同一任务脚本中的多个显式 `WaitCustomString` 锚点和 `PlayTimeline` 证据，只恢复锚点之间、归属唯一的缺口，并放回对应分支 |
| 当时的候选校正 | 本地历史审计的 v2 候选新增 456 行，v3 收敛为 437 行、193 块，避免在重复前缀或多脚本归属不明确时串入对白。这些数量是同一份历史输入的结果，未来版本应根据实际变化比较 |
| 本次预检补充 | 支持仅有 `TextMapMainCHS.json` 的输入，明确拒绝缺任务表和零台词候选，并记录解析器修订与 timeline 恢复行数 |

新的缺漏需要先定位原始脚本、台词 ID 和所属任务，把最小样本加入 `tests/test_data.py` 再修解析器。`data/audit-*.py` 属于旧的本地实验，正式流程使用仓库里的解析器和本工具。

## 记录四种版本

- **来源提交 SHA** 固定完整 40 位 Git 提交，作为此次下载和重建的身份。
- **数据覆盖的游戏版本** 来自上游提交标题。例如 `OSPRODWin4.7.0_D17180467_A17176203_L17175930` 对应 4.7。相同 4.7 下也有多个热修提交，必须保留完整标题和 SHA。
- **任务首次开放版本** 是逐任务字段，优先使用已核验的 `backend/resources/mission_versions.json`，与任务 ID、名称、章节、地区、类型一起匹配。旧任务会出现在新版本全量数据里，当前提交标题不足以证明其首次开放版本。
- **应用版本与解析器修订** 分别来自 `backend/__init__.py` 和 `backend/data.py` 的 `IMPORTER_VERSION`。数据更新可以继续使用同一应用版本，解析器修复应记录代码提交。

标题为可识别的 `OSPRODWin<major>.<minor>.<patch>_D..._A..._L...` 时，工具提取游戏版本。其他标题保留版本为空，AI 需要读取提交与正式更新依据，在来源 JSON 中补 `game_version` 和 `game_version_source`（HTTPS）。未知任务首次版本保留 `unknown`，可以在审阅候选时另外核验。

## 准备独立的维护目录

下面示例在源码根目录、PowerShell 中执行，使用已安装依赖的 Python 3.12 环境。每条外部命令都检查退出码，异常时保留报告并修复对应阶段。`--report` 是全局参数，放在子命令前。

```powershell
$Python = '.\.venv\Scripts\python.exe'
$CaseId = [guid]::NewGuid().ToString('N')
$Case = Join-Path (Get-Location).Path "data/maintenance/$CaseId"
New-Item -ItemType Directory -Path $Case -ErrorAction Stop | Out-Null
git rev-parse HEAD
git status --short
& $Python scripts/data_tools.py --help
```

`data/` 已被 Git 忽略。仓库提交解析器、回归测试、公开版本元数据与维护文档。维护报告、候选数据库、密钥和个人配置保存在各自工作目录。

## 1. 固定来源并核对提交标题

```powershell
& $Python scripts/data_tools.py --report "$Case/upstream.json" upstream
$Upstream = Get-Content -LiteralPath "$Case/upstream.json" -Raw | ConvertFrom-Json
$Upstream
```

`upstream` 只请求 GitHub 的提交信息。可用 `--commit <完整SHA>` 检查指定历史提交，或用 `--repository owner/repository` 指定另一个数据源。默认仓库为 `DimbreathBot/TurnBasedGameData`。

核对版本、正式服数据来源和目标更新范围后，从已记录的固定提交下载原始 ZIP 并解压。

```powershell
$SourceZip = Join-Path $Case 'source.zip'
$SourceDirectory = Join-Path $Case 'source'
$SourceUrl = "https://codeload.github.com/$($Upstream.repository)/zip/$($Upstream.commit)"
Invoke-WebRequest -Uri $SourceUrl -OutFile $SourceZip -ErrorAction Stop
Get-FileHash -LiteralPath $SourceZip -Algorithm SHA256
Expand-Archive -LiteralPath $SourceZip -DestinationPath $SourceDirectory -ErrorAction Stop
& $Python scripts/data_tools.py --report "$Case/source-audit.json" inspect-source --source "$SourceDirectory"
```

预检核对中文 TextMap、任务表、台词表、脚本格式、任务类型和 timeline 数量，并计算文件内容指纹。下载地址中的 SHA、提交记录和压缩包哈希需要一并保留。手动提供来源 JSON 只是登记证据，AI 仍需确认输入确实取自该提交。既有旧包缺少来源记录时，应如实标记未知，保留旧包的哈希和历史发布说明。

## 2. 生成候选并与当前快照比较

从待维护实例的 `data/current.json` 定位基线，下面以源码本地实例为例。服务端实例应使用该实例的快照或其一致副本。

```powershell
$Current = Get-Content -LiteralPath data/current.json -Raw | ConvertFrom-Json
$Baseline = (Resolve-Path -LiteralPath "data/snapshots/$($Current.snapshot)/story.db").Path
$Candidate = Join-Path $Case 'candidate.db'
& $Python scripts/data_tools.py --report "$Case/baseline-audit.json" audit --database "$Baseline"
& $Python scripts/data_tools.py --report "$Case/build-report.json" build --source "$SourceDirectory" --output "$Candidate" --provenance "$Case/upstream.json" --baseline "$Baseline"
```

首次实例没有基线时，省略 `--baseline` 即可。`build` 生成全新候选文件，沿用正式解析器，并记录来源 SHA、标题、游戏覆盖版本、输入指纹与解析器修订。已有的输出文件会被保留，应为重试选择新候选路径。

重点审阅报告中的任务、对白、片段增减，移除或改写的旧对白、新增任务、未知版本任务，以及可复用的剧情块数量。数量下降或未知版本都需要结合此次来源变化核对，工具会列出变化供判断。

构建时只解析剧情与全文检索索引。向量步骤另行执行，候选暂时出现 `no_vector_index` 警告是正常状态。

## 3. 按问题选择修复方式

| 问题 | 操作 |
| --- | --- |
| 漏台词、错任务、错分支、文本清洗错误 | 修解析器和对应最小回归样本，使用同一固定来源重建新的候选，再比较 |
| 只有首次开放版本缺失 | 核实任务页面，更新公开元数据后生成元数据修复副本 |
| 上游 TextMap 或任务表缺失、脚本格式变化 | 修输入或适配解析器，重新执行预检和候选构建 |
| 向量数量、参数或缓存不兼容 | 在候选与独立工作目录处理索引，核对模型和签名再继续 |

仅补任务版本时可执行下面命令。它会通过 SQLite backup 生成一致副本，只为身份匹配且版本未知的任务填入已核验记录，保留已有人工标注与向量数据。

```powershell
& $Python scripts/data_tools.py --report "$Case/metadata-repair.json" repair-metadata --database "$Baseline" --output "$Case/metadata-candidate.db"
```

可用 `--metadata <元数据JSON>` 指定待审阅的元数据文件。补充 BWIKI 元数据的方法见 `scripts/sync_versions.py --help`，同步结果仍需要审阅新任务身份与依据。

## 4. 处理向量并验收候选

现有 `Runtime.build_index` 会按模型、前缀、额外语义参数和文本内容哈希查找 `vector-cache.db`。缓存存在且参数一致时，只请求缺失向量。它也会从传入库和当前活动快照中读取身份匹配的旧向量，先补齐缓存，再计算候选缺失内容。

新建候选的向量缓存目录可能为空。AI 在触发嵌入服务前先核对旧快照、缓存位置、模型、维度和预计缺失数量，再采用可复用缓存。保留原索引作为恢复依据，特别注意使用用户自己的密钥及服务调用量。

完成候选索引后重新执行审计和比较。

```powershell
& $Python scripts/data_tools.py --report "$Case/candidate-audit.json" audit --database "$Candidate"
& $Python scripts/data_tools.py --report "$Case/comparison.json" compare --baseline "$Baseline" --candidate "$Candidate"
```

审计检查 SQLite 完整性、必要表、空库、台词与剧情块关系、全文检索对应关系、任务版本分布、向量数量和索引身份。旧数据缺少来源记录或解析器修订会显示警告，结构和索引破损会报错并返回退出码 1。

`compare` 中的 `reusable_chunks` 表示候选文本哈希在旧库中出现的块数。实际向量可复用性还取决于旧索引是否完整以及模型身份是否一致。

用候选单独启动测试实例，检查开场空间站卡芙卡对白、匹诺康尼 timeline 场景、一个有多个玩家选项的任务和此次新增任务。核对搜索、完整上下文、分支回应和任务版本。语义检索需要另测真实查询向量。

## 5. 交付与部署记录

离线生成的 `.db` 候选可用以下命令导出标准数据包。此步骤复用程序的清单与哈希格式，在临时维护目录完成校验与打包。

```powershell
& $Python scripts/data_tools.py --report "$Case/package-report.json" package --database "$Candidate" --output "$Case/hsr-data-candidate.zip"
```

随后在统一后台“星轨档案”选择“标准数据包”上传，生成服务器端候选并再次审核。缺少索引时可在后台配置自己的嵌入服务，为候选复用或计算向量。按顺序查看差异、审计与检索抽查结果，再选择激活。后台使用生成候选时的在线快照作为基线，在线数据已变化时需要重新生成候选。

AI 交付应包含来源仓库、固定 SHA、完整提交标题、数据覆盖版本、应用代码提交、解析器修订、增减报告、未知版本清单、向量完整性与实际查询验证结果。将验证通过的候选通过管理后台的候选审阅与启用流程安装，保留前一快照并检查线上相同场景。GitHub 数据包与线上实例使用同一份通过验证的产物。

先前的成功参考为 2026-11-14 本地活动快照的只读审计，包含 2,866 个任务、180,363 行对白、100,496 个片段与完整向量，台词关系及 FTS 对应检查无缺口。该旧库未登记来源 SHA 和解析器修订，应继续如实记录这一限制。

本次 0.6.0 部署后，服务器上的相同快照也通过完整审计，未知首次版本任务为 0。已执行后端全套回归及后续工具的针对性回归、35 项前端测试、前端生产构建、公开关键词检索和源站认证拒绝检查。真实个人账号的 CF 登录及真实嵌入服务调用另行验证，诊断工具和打包过程使用临时样本验证了候选生成、打包、上传格式安装与回滚。
