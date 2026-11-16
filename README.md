# 标讯配置台 ai-market-demo v1.0

一个自带 Web 界面的**标讯过滤 + 多渠道推送**工具：设规则 → 建多个任务 → 每个任务推到各自的群。

- 零依赖：纯 Python 3.12 标准库，无第三方包，镜像仅 88MB
- 全渠道：钉钉 / 企业微信 / 飞书 / 自定义 HTTP
- 先预览后推送：命中结果先看，勾选哪些再发，绝不自动骚扰群
- 每日强制更新：默认每天 08:30 从一手官网刷新缓存，任务再按各自 cron 过滤和推送
- 定时可开关：任务级 cron + 全局总开关；模板首次部署默认关闭，生产可持久化开启

访问：`http://<服务器IP>:8080/`　账号 `admin` / 密码见第 4 节。

---

## 一、目录结构

```
ai-market-demo/
├── server.py            HTTP 服务 + 全部 API（含 Basic Auth）
├── engine.py            规则引擎（include AND / exclude OR）+ 按任务去重
├── channels.py          四个渠道的投递适配器（含签名/业务码校验）
├── render.py            消息模板（每条标讯 + 联系方式汇总）
├── fetcher.py           数据源（离线样例 / 实时采集）
├── scheduler.py         cron 解析 + 后台调度线程
├── selftest.py          46 项回归自测（改代码后必跑）
├── ui.html              配置台前端（单文件）
├── deploy/
│   ├── Dockerfile.demo           镜像构建
│   ├── compose.standalone.yml    独立部署编排（推荐）
│   └── compose.demo.yml          并入现有 compose 栈的示例片段
└── data/                运行时可变内容（挂载到容器 /app/data）
    ├── *.example.json   空模板：仓库里只有这些，clone 后复制成实际配置
    ├── state/           每任务去重（运行时自动生成）
    ├── outbox/          投递记录（运行时自动生成）
    └── settings.json    定时总开关状态（运行时自动生成）

仓库**不含** `tasks.json` / `channels.json` / `secrets.json` / `auth.json` /
`tenders_fixture.json` 的实际内容，它们由模板复制生成并被 `.gitignore` 排除。
```

---

## 二、快速开始

### 步骤 0：从 Git 仓库 clone 时，必须先初始化配置

仓库**不含任何业务数据、样例标讯、任务规则和真实凭据**，`data/` 下只有空的 `*.example.json` 模板。clone 后先做一次：

```bash
cp data/channels.example.json         data/channels.json         # 填真实 webhook
cp data/secrets.example.json          data/secrets.json          # 填加签密钥
cp data/auth.example.json             data/auth.json             # 填登录密码 sha256（见第 5 节）
cp data/tasks.example.json            data/tasks.json            # 任务列表，初始为空，在界面里新建
cp data/tenders_fixture.example.json  data/tenders_fixture.json  # 离线样例，初始为空数组
```

这五个文件均被 `.gitignore` 排除，改动不会误提交。**没做这一步就启动，页面能打开但推送会静默失败、预览也没有数据。**

> 仓库定位是「纯代码 + 空模板」，可直接对外发布；需要开箱即用的完整配置，请使用内网交付包。

### 方式 A：独立部署（推荐）

在**包含 server.py 的目录**执行：

```bash
docker compose -f deploy/compose.standalone.yml up -d --build
```

### 方式 B：并入已有 compose 栈

把 `deploy/compose.demo.yml` 里的 `ai-market-demo` **服务段**复制进目标项目的 `compose.yml`（只加不改其他服务），然后：

```bash
docker compose up -d --build ai-market-demo
```

> 该段使用 `./demo-data:/app/data` 卷。首次部署前先初始化：
> `mkdir -p demo-data && cp -r data/* demo-data/`

### 方式 C：离线部署（无网络 / 无构建环境）

```bash
docker load -i ai-market-demo-1.0-image.tar.gz     # 导入镜像
docker compose -f deploy/compose.standalone.yml up -d   # 不重新构建
```

### 部署后自查

```bash
curl -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/health   # 期望 200（免认证）
docker inspect --format '{{.State.Health.Status}}' ai-market-demo        # 期望 healthy
```

---

## 三、登录

浏览器打开 `http://<IP>:8080/`，弹出登录框：

| 项 | 值 |
|---|---|
| 账号 | `admin`（可在 `data/auth.json` 改） |
| 密码 | 你在 `data/auth.json` 里设置的密码；仓库不含默认密码，见第 5 节 |

页面与全部 `/api/*` 都受 Basic Auth 保护，仅 `/api/health` 免认证（供容器健康检查使用）。

---

## 四、功能

### 任务与规则

每个任务 = 一套过滤规则 + 一组推送渠道 + 一条定时表达式。可以有任意多个任务，互不去重干扰。

规则界面是**勾选 + 关键词标签**，不需要写 JSON：

| 区块 | 作用 |
|---|---|
| 地区（可多选） | 命中其一即可，内置 37 个主要城市，另自动合并真实数据里出现的地区 |
| 标题关键词 | 任一关键词出现在标题/摘要即算命中 |
| 状态 | 公开招标 / 采购公告 / 征集 等 |
| 预算金额 | 下限 / 上限（万） |
| 发标时间 | 最近 N 天内 |
| 排除已截止 / 排除标书获取已截止 | 勾上即过滤，时间基准是**每次求值时的当前时间** |
| 采购人包含 / 排除 | 白名单与黑名单关键词 |
| 淘汰关键词 | 标题含任一即排除 |

高级用户可展开「高级 JSON」直接写规则：

```json
{
  "include": [
    {"field": "region", "op": "in", "value": ["上海", "深圳"]},
    {"field": "title", "op": "contains_any", "value": ["服务器", "GPU", "智算"]},
    {"field": "budget_num", "op": "gte", "value": 500000},
    {"field": "deadline_dt", "op": "window", "value": {"from": "now"}}
  ],
  "exclude": [
    {"field": "purchaser", "op": "contains_any", "value": ["公安局", "法院", "税务"]}
  ]
}
```

- `include` 之间是 **AND**（全部满足才命中），`exclude` 之间是 **OR**（命中任一即淘汰）
- 可选 `op`：`contains_any` / `regex` / `in` / `eq` / `gte` / `lte` / `between` / `window`
- 可用字段：`region` `title` `summary` `text`(标题+摘要) `purchaser` `status` `budget_num` `published_dt` `deadline_dt` `file_start_dt` `file_end_dt` `bid_open_dt`

### 推送渠道

四种类型，各自签名方式不同：

| 类型 | 加签 |
|---|---|
| 钉钉 `dingtalk` | 支持加签（`timestamp\nsecret` HMAC-SHA256），也兼容仅关键字校验 |
| 企业微信 `wecom` | 不需要加签，webhook 里带 key |
| 飞书 `feishu` | 支持加签；消息自动压成纯文本（飞书 text 消息不渲染 markdown） |
| 自定义 `custom` | 裸 POST JSON，用于对接内部系统 |

配好后点「发送测试消息」即可验证连通性，**不必等到真实推送才发现配置错**。

### 预览与推送

1. 选任务 → 点「预览」→ 看命中列表（按截止时间排序，标出 `新` / `已推`）
2. 勾选要发的项（支持全选）
3. 点「推送」→ 逐渠道发送，结果实时显示

推送结果按**业务码**判定，不只看 HTTP 200。钉钉加签错、关键字不匹配、被限流都会返回 HTTP 200 + 非 0 errcode，这种情况会记为失败、**不写入去重状态**，下次会重推，不会静默丢消息。

### 定时推送

需要**两个开关同时满足**才会自动推送：

1. 任务编辑页勾选「启用此任务的定时推送」，设周期（工作日 / 每天 / 自定义 cron）和时间，保存
2. 「预览与推送」页点「开启自动推送」

总开关状态持久化在 `data/settings.json`，容器重启不会丢。cron 为 5 段标准格式（分 时 日 月 周），例：`0 9 * * 1-5` = 工作日 9:00。状态栏会分别显示最近扫描、最近触发及触发结果；“无新标讯”表示调度已正常执行，只是去重后没有需要发送的新内容。若没有任何任务开启定时，系统会拒绝开启总开关，避免出现“显示开启但实际无任务”的假运行状态。

生产默认设置 `AUTO_UPDATE=1`、`UPDATE_SCHEDULE="30 8 * * *"`：每天 08:30 强制调用 `LIVE_MODULE_PATH` 指向的采集器，从一手官网刷新最近 21 天标讯缓存；更新失败会写明错误且保留上一份缓存，不会把旧数据冒充“今日已更新”。可通过 `GET /api/updater` 查看更新时间和采集统计，通过 `POST /api/updater/run` 手动立即刷新。

---

## 五、配置文件

全部在 `data/`，改完**无需重建镜像**，重启容器或等下次读取即生效（配置每次请求实时读盘）。

### channels.json

```json
{
  "id": "wh-sales",
  "name": "销售群（钉钉）",
  "channel": "dingtalk",
  "webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
  "secret_ref": "sec:dingtalk_sales"
}
```

`secret_ref` 是指向 `secrets.json` 的键名；不需要签名时填 `null`。界面和 API **永不回显密钥明文**。

### secrets.json

```json
{ "sec:dingtalk_sales": "<你的加签密钥>" }
```

键名必须与 `channels.json` 的 `secret_ref` 完全一致。

### auth.json

```json
{ "username": "admin", "password_sha256": "<64位sha256，生成方法见下>" }
```

### 改密码

密码以 sha256 存储，不落明文。改法：

```bash
python3 -c "import hashlib;print(hashlib.sha256('你的新密码'.encode()).hexdigest())"
# 把输出的 64 位串填进 data/auth.json 的 password_sha256，然后重启容器
docker restart ai-market-demo
```

> 若把 `password_sha256` 置空或删掉 `auth.json`，认证会被**关闭**（任何人可访问），公网环境务必保留。

---

## 六、接入真实采集数据

未配置 `LIVE_MODULE_PATH` 时读取 `data/tenders_fixture.json` 离线数据；正式环境按下述方式挂载一手官网采集器后，每日更新与实时预览使用真实数据：

1. 把采集脚本放到宿主机（例如 `/opt/collectors/shanghai_tender_push.py`）
2. 在 compose 里挂载并指定路径：

```yaml
    environment:
      LIVE_MODULE_PATH: "/opt/collectors/shanghai_tender_push.py"
    volumes:
      - /opt/collectors/shanghai_tender_push.py:/opt/collectors/shanghai_tender_push.py:ro
```

3. 重启容器；每日更新调度会调用真实采集器刷新本地缓存，预览时也可勾选「实时数据」立即采集

采集脚本需提供 `TERMS`、`search(term,start,end)`、`detail(candidate)`、`ident(item)`。本仓库不包含特定站点采集器，避免把站点适配、真实业务口径或渠道逻辑混入通用配置台；请自行实现并遵守目标站点服务条款。字段名需对齐 `tenders_fixture.json`，缺字段不会导致服务崩溃，但配置了对应必选规则时会按不满足处理。

每日更新本身**不调用大模型**。已预留可选的 OpenAI 兼容配置环境变量：`LLM_ENABLED`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`；默认 `LLM_ENABLED=0`，未启用时对采集、规则过滤和推送没有任何影响。接口只返回“是否已配置”，不会回显 API Key。

---

## 七、运维

```bash
docker logs -f --tail 100 ai-market-demo          # 日志
docker inspect --format '{{.State.Health.Status}}' ai-market-demo
docker restart ai-market-demo                     # 改配置后重启（多数配置其实无需重启）
```

- **升级**：替换源码 → `docker compose up -d --build ai-market-demo`，`data/` 卷里的配置和状态不受影响
- **备份**：只需备份 `data/` 整个目录（任务、渠道、密钥、去重状态、投递记录都在里面）
- **投递记录**：`data/outbox/`，超过 2MB 自动轮转，不会撑爆磁盘

---

## 八、排障

| 现象 | 原因 / 处理 |
|---|---|
| 容器一直 `health: starting` | healthcheck 打到了需要认证的端点。必须指向 `/api/health` |
| 浏览器无凭据访问得到空回复 | 旧版本的 `WWW-Authenticate` 用了中文 realm（HTTP 头必须 ASCII），已修 |
| 钉钉报 `errcode: 310000` | 机器人开了「自定义关键字」，消息正文必须含该关键字；或加签密钥不对 |
| 钉钉报 `errcode: 130101` | 发送频率超限（每分钟 20 条），已按上限截断，稍后重试 |
| 飞书消息出现 `**` `#` 符号 | 旧版本问题，现已自动压平为纯文本 |
| 企微无反应 | `webhook` 里 `REPLACE_WECOM_KEY` 还是占位，需换成真实 key |
| 保存任务报 400 | 检查 cron 是否合法（`99 99 * * *` 会被拒）、任务 id 是否只含字母数字下划线横线 |
| 删除渠道报 409 | 该渠道仍被任务引用，先解绑任务或改绑其他渠道 |
| 推送显示成功但群里没消息 | 现已按业务码判定，若仍显示成功请查 `data/outbox/` 的实际响应体 |

---

## 九、安全提醒（重要）

本仓库**已脱敏**：不含真实 webhook、加签密钥、登录密码、业务任务规则或样例标讯，只保留空的 `*.example.json` 模板。所有实际配置文件均被 `.gitignore` 排除。

需要注意的只有**你本地运行环境**：按第 2 节步骤 0 填好之后，`data/secrets.json`（加签密钥）、`data/channels.json`（webhook 里的 access_token，拿到即可往群里发消息）、`data/auth.json`（密码 hash）就在本机磁盘上了。这三个文件：

- 不要提交，也不要随源码一起打包外发
- 内网交付包里是带真实值的，仅限内部留存
- 当前是 HTTP 明文传输，公网访问请套一层 HTTPS 反向代理

当前为 HTTP 明文传输，内网使用无碍；若需公网访问，请在前面套一层 HTTPS 反向代理。

---

## 十、自测

改动任何代码后，跑一遍回归：

```bash
python3 selftest.py        # 46 项，覆盖时间基准、规则运算符、去重顺序、cron、定时字段保留、联系方式排版、预算归一、路径穿越等历史坑
```

容器内跑：

```bash
docker cp selftest.py ai-market-demo:/tmp/selftest.py
docker exec -w /app -e PYTHONPATH=/app ai-market-demo python3 /tmp/selftest.py
```
