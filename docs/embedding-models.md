# 模型配置与剧情索引构建

在线版与现有预计算数据包采用 `BAAI/bge-m3`，原生 1024 维，查询和文档前缀均为空。本站使用硅基流动生成该索引，服务商仅为配置示例，不构成推广。使用者可以选择提供相同模型、匹配参数和兼容接口的服务。

## BGE-M3 与免费服务

截至 2026-11-14，两个平台的官方说明如下。免费服务的额度、限速和价格随平台政策调整。

| 平台 | 模型标识 | 免费使用方式 |
| --- | --- | --- |
| 硅基流动 | `BAAI/bge-m3` | 标准版当前标价免费，另有单独计费的 `Pro/BAAI/bge-m3` |
| Cloudflare Workers AI | `@cf/baai/bge-m3` | 可使用 Workers AI 每日 10,000 Neurons 的免费额度，额度由账户内 Workers AI 调用共享 |

来源为平台官方价格与模型页面。[硅基流动价格](https://www.siliconflow.cn/pricing) · [Cloudflare BGE-M3](https://developers.cloudflare.com/workers-ai/models/bge-m3/) · [Cloudflare 额度与计费](https://developers.cloudflare.com/workers-ai/platform/pricing/)

## 使用现有索引

1. 在“语义密钥”中填写自己的 API Key。默认请求硅基流动的 `https://api.siliconflow.cn/v1/embeddings`。
2. 使用其他兼容服务时展开“其他兼容平台接口地址”，填写其 `/embeddings` 地址或基础地址。程序会按当前索引记录的模型、查询前缀、维度和额外参数发送查询请求。
3. 核对服务确实提供相同的 BGE-M3 模型和 1024 维输出，再用已知台词、人物和场景抽查描述检索结果。跨平台输出与预处理的一致性仍需实际核验。

默认硅基流动连接优先从浏览器直连，失败时由本站服务端在内存中代发。自定义接口当前使用浏览器直连，需要正确配置 CORS，允许页面来源以及 `Authorization`、`Content-Type` 请求头。密钥保存在当前浏览器中。

### Cloudflare 接入要点

Cloudflare 提供的 OpenAI 兼容嵌入接口为以下地址，其中账户 ID 使用自己的值。

```text
https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1/embeddings
```

Cloudflare 的模型名称为 `@cf/baai/bge-m3`，现有数据包中记录的名称为 `BAAI/bge-m3`。接入现有索引时，兼容转发服务需要将请求里的 `BAAI/bge-m3` 映射到 Cloudflare 对应模型，并返回 OpenAI 兼容的 `data[].embedding` 数组。浏览器直连还需要服务支持相应 CORS。

可以让自己的 AI 按 Cloudflare 文档配置兼容网关，处理模型名映射和 CORS。确认接口格式、输出维度和查询结果后，再在“其他兼容平台接口地址”填写该网关地址。[Cloudflare OpenAI 兼容接口](https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/)

## 使用其他嵌入模型重建索引

更换模型时，需要为全库剧情片段计算新向量，并让查询采用相同模型与参数。两个模型即便维度相同，其向量也需要分别建立索引。项目按模型、维度、前缀和额外语义参数区分索引身份。

### 1. 创建独立的本地实例

先按源码运行说明安装依赖并构建网页，再在仓库根目录执行以下 PowerShell 命令。为实验使用独立目录，保留原有可用数据。

```powershell
$ModelData = Join-Path (Get-Location).Path ("build/model-lab-" + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe launcher.py --data-dir "$ModelData"
```

### 2. 配置目标模型并导入剧情

在本地“管理 → 搜索 API”填写提供 OpenAI 兼容 `/embeddings` 的服务基础地址、API Key 和目标嵌入模型名称。按模型说明设置查询与文档前缀、额外参数、批次和并发。维度留空可自动检测输出维度（内部配置为 0），明确填写维度前应确认模型支持相应请求参数。

使用自建模型服务时，先确保接口能接收一组文本并按原顺序返回浮点向量。通过“保存并测试连接”检查凭据、模型与输出维度。

随后在“管理 → 数据与更新”导入原始剧情数据或已有 `hsr-data-*.zip`。原始数据导入在已配置密钥时可能直接开始构建索引，应先核对平台额度。导入已有 BGE-M3 数据包后，模型不匹配会显示索引不可用，需要执行下一步。

### 3. 建立全库向量索引

在本地“数据与更新”中执行“建立 / 补全向量索引”。构建器使用当前配置，按内容哈希复用同一模型身份的缓存，仅计算缺失向量。换用其他模型时会重新计算相应内容，旧模型缓存与新模型索引分别保存。

全库构建的调用量远高于一次查询。免费额度与速率限制也适用于构建任务，可以降低并发和批次，按任务状态暂停或继续。完成后确认剧情片段与索引条数一致，索引模型和维度正确，并用多条已知剧情测试描述检索及上下文。

### 4. 导出与分享

通过本地管理界面导出标准数据包，记录模型名称、维度、前缀、额外语义参数、剧情来源版本及验证结果。接收者导入后，也需配置相同模型与参数，才能使用该索引进行查询。源文件或分段规则变化时，按数据维护流程重新生成候选、比较内容并补齐索引。[数据维护流程](data-maintenance.md)

### 可交给 AI 的提示词

```text
请先阅读 docs/embedding-models.md 和 docs/data-maintenance.md，
使用我提供的嵌入服务与模型，在独立数据目录构建星轨档案剧情索引。
先检查接口格式、模型名称、维度、查询和文档前缀及调用额度，
再导入指定剧情数据，计算该模型的完整向量索引。
完成后核对向量条数并抽查描述检索，导出标准数据包，
记录剧情来源、模型参数、实际验证和预计持续调用成本。
```
