# 管理后台与源站认证

本站沿用个人博客写作后台的 Cloudflare Access 应用，统一入口为主站 `/admin/`。文章编辑、图片上传、星轨档案设置、候选数据与任务状态都从该路径访问。

## 两层身份校验

Cloudflare Access 在边缘完成登录与准入。博客后台与星轨后端还会分别使用固定团队的 JWKS 公钥校验令牌签名、RS256 算法、签发者、目标 AUD、有效期、用户标识及邮箱白名单。邮箱请求头本身不能建立身份。官方验证方式见 [Cloudflare 文档](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)。

星轨后端通过环境变量启用此模式。

```dotenv
HSR_ADMIN_AUTH=cloudflare
HSR_CF_TEAM_DOMAIN=https://your-team.cloudflareaccess.com
HSR_CF_AUDIENCE=your-writing-access-application-audience
HSR_CF_ALLOWED_EMAILS=you@example.com
HSR_ACCESS_CODE=
HSR_PUBLIC_PREFIX=/tools/hsr/
```

CF 模式下配置缺失会导致启动失败，管理请求缺少有效身份会返回 403，旧管理员口令登录及旧直接安装任务的启动或恢复会被拒绝。公开检索继续使用只读接口。本地便携版通过启动器的一次性令牌建立本地会话。

独立自托管也支持原有 `HSR_ADMIN_AUTH=code` 与至少 12 字符的 `HSR_ADMIN_CODE`，由部署者自行配置 HTTPS 和访问策略。

## 统一入口的代理约定

博客后台服务监听 `127.0.0.1:8091`。认证通过后，仅将白名单内的 `/admin/api/hsr/*` 转发到 `127.0.0.1:8765/api/v1/admin/*`，同时传递 Access 令牌供后端再次验证。代理目标和端口固定。

部署时让现有 CF 写作应用覆盖 `/admin`、`/admin/` 及所有子路径，包括 `/admin/assets/*` 与 `/admin/api/*`。检查实际应用的路径匹配及账号准入策略。

后台脚本与样式由认证后的服务返回，响应使用 `private, no-store`。服务器 Nginx 将旧 `/assets/admin.js`、`/assets/admin.css`、`/assets/writing.js` 及公共站点下的星轨管理接口和后台资源路径封闭。源站监听回环地址，经现有 Tunnel 回源。

## 数据维护

上传原始 ZIP、填写固定上游 SHA 或安装标准数据包时，先生成候选。候选报告包含来源、质量检查、未知版本、差异和索引状态。需要索引时，先核对模型、可复用向量及服务调用量，再为候选构建索引。

发布会重新审计候选，并检查生成候选时的在线基线是否仍然有效。当前在线索引完整而候选缺少匹配索引时，默认要求先补索引。维护者可以明确选择仅关键词发布。快照切换保留上一版用于回滚。

后台提供固定的数据操作，AI 的代码修复、来源调查与部署通过维护流程执行。[数据维护与修复流程](data-maintenance.md)

## 验证入口

修改认证或代理后，检查未登录、伪造邮箱头、伪造签名、过期令牌、错误 AUD/签发者、非白名单账号、旧管理员口令、静态资源的编码点路径及直接回源。认证后的页面还应验证文章编辑、候选任务和分块上传。

`tests/test_access.py` 与 `tests/test_candidates.py` 覆盖源站认证和候选隔离的回归。实际 CF 登录状态与边缘策略需要在部署环境验证，并记录未完成的验证项。
