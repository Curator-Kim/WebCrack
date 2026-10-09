# WebCrack-plus

WebCrack-plus 是基于 [yzddmr6/WebCrack](https://github.com/yzddmr6/WebCrack)
原版 v2.2 的增强版本，用于 Web 登录页面的弱口令及万能密码检测。
保留原版作者署名、字典生成和 CMS 适配能力，扩展接口发现、请求并发、
结果判定和自动化测试。命令行入口保留为 `webcrack.py`，兼容既有调用方式。

仅在获得明确许可的系统中使用。结果文件含明文凭据，请限制访问并妥善保管。

## 相比原版的改进

以下对比以本仓库原版 v2.2 代码为基准，而不是与本增强分支的上一提交对比。

| 改进项 | 原版 | WebCrack-plus |
| --- | --- | --- |
| 检查并发 | URL 与候选登录请求顺序执行 | 有界线程池；`-t` 控制 URL 并发，`-c` 控制每个 URL 的候选请求并发 |
| 识别复用 | 缺少任务内接口计划复用机制 | 复用 Axios、jQuery、静态接口、CMS 和站点配置的识别结果；表单动态字段仍刷新，结构变化时重新识别 |
| 登录接口 | 主要依赖传统 HTML 表单和 CMS 特征 | 增加 fetch、Axios、jQuery 的静态请求识别，同源登录入口发现及精确站点配置 |
| 成功判断 | 主要参考失败响应长度、关键词及长度复核 | 明确成功证据、失败基线排除、严格 JSON 规则和独立会话复核；长度变化不单独证明成功 |
| 会话与停止逻辑 | 单任务会话及原有关键词判断 | 工作线程会话隔离、表单 Cookie/隐藏字段刷新；限流、锁定、异常或成功复核后停止提交新候选 |
| 命令行 | 交互输入 URL 或文件 | 增加 URL/文件、输出、超时、延迟、代理、随机请求头及两级并发参数；保留交互模式 |
| 结果与日志 | 日期目录日志 | 增加 UTF-8 TSV 成功结果汇总、输入顺序保持、重复 URL 去重、线程局部日志 ID 和日志写入锁 |
| 请求头 | 静态 UA 列表，并随机设置转发 IP 头 | 使用 fake-useragent 生成 UA；默认不伪造 `X-Forwarded-For` / `Client-IP` |
| 回归验证 | 缺少当前的自动化检查集 | 76 项回归覆盖解析、判定、并发、识别复用、动态令牌及本地 HTTP；另有 HTML/JSON 集成检查 |

并发并不保证所有站点都更快。页面刷新、网络延迟、连接开销和服务端限制
都会影响吞吐；本版本使用同步 `requests` 与有界线程池，不是 asyncio 客户端。
不执行页面 JavaScript，不自动处理验证码、前端密码加密或动态 JSON 令牌刷新。

## 快速开始

当前远程仓库地址保持不变；克隆时可使用新的本地目录名：

```bash
git clone https://github.com/Curator-Kim/WebCrack.git WebCrack-plus
cd WebCrack-plus
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 webcrack.py --help
```

示例（请替换为已获许可的测试页面）：

```bash
# 单个 URL 内同时执行最多 5 个候选登录请求
python3 webcrack.py -u http://127.0.0.1:8000/login -t 1 -c 5

# 同时处理 2 个 URL，每个 URL 最多 3 个候选请求
python3 webcrack.py -f url.txt -t 2 -c 3 -o results/output.txt

# 顺序执行
python3 webcrack.py -f url.txt -t 1 -c 1

# 不带参数时保留交互式 URL/文件输入
python3 webcrack.py
```

输入文件每行一个 URL；忽略空行、`#` 开头的注释及配置中的忽略域名。
完全相同的 URL 保留第一次出现的位置；不同查询参数的地址不合并。

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `-u, --url` | 单个页面 URL，与 `-f` 互斥 |
| `-f, --file` | URL 列表文件，与 `-u` 互斥 |
| `-o, --output` | 成功结果文件；默认 `output.txt`，覆盖写入 |
| `-t, --threads` | 同时处理的 URL 数；默认 5，必须为正整数 |
| `-c, --concurrency` | 每个 URL 的候选请求并发数；默认配置为 5，必须为正整数 |
| `--timeout` | 请求超时秒数；默认配置为 10，必须为有限正数 |
| `--delay` | 每个请求后的等待秒数；默认配置为 0.03，必须为有限非负数；不是全局限速 |
| `--proxy` | 完整 HTTP/HTTPS 代理 URL |
| `--no-random-headers` | 使用配置中的默认请求头 |

配置项位于 `conf/config.py`，基础密码字典为 `conf/password_list.txt`。
普通及成功日志位于 `logs/{date}/`；汇总输出见下文。

## 成功结果导出

运行结束后，将通过复核的成功结果写入 UTF-8 制表符分隔文件，包含
`url`、`username`、`password` 三列。默认文件为当前目录的 `output.txt`，
可通过 `-o/--output` 指定路径，也可修改 `conf/config.py` 的
`logConfig["output_filename"]`。每次运行覆盖输出文件；没有成功结果时仅写表头。
原有日期日志保持不变。结果包含明文凭据，请妥善保管。

```bash
python3 webcrack.py -f url.txt -o results/output.txt
```

## 登录结果判定

初判和复核使用同一规则：失败提示、仍存在的密码表单、HTTP 异常、
限流或锁定响应均不记为成功。响应长度、跳转或 Cookie 变化不单独作为成功依据。
使用 CMS 的成功正文标记，或在 `crackConfig["success_words"]` 配置目标的明确成功标记；
JSON 接口可配置 `json_success_fields`（支持嵌套对象路径，全部严格匹配）。
失败基线也含有的成功标记会被排除；没有明确证据则标记为不确定，不导出。
复核通过独立会话重新获取登录页面及表单状态。无通用规则能证明任意网站已登录，
请按页面实际响应配置明确标记。

## JSON 与 jQuery 登录接口

支持的静态请求形式和限制见下一节。JSON 请求保持 `application/json`，
jQuery `serialize()` 请求保持表单编码；接口必须同源且映射唯一。
Axios 包装器与 jQuery 成功回调中的已知条件会作为当前接口的判定规则，
不会将所有站点的 `code=200` 统一视为成功。JSON 规则支持点分隔的嵌套对象路径。
401 视为凭据失败，429 停止任务；返回 Token 不自动证明权限有效。

## 扩展识别与配置式适配

识别流程：精确站点配置 → 页面/脚本静态识别 → 一个明确的同源登录链接 → 传统表单。
通用静态识别器位于 `parse/recognizers.py`，资源读取位于 `parse/resources.py`；
之前验证过的 jQuery serialize 和 Axios 包装响应适配仍保留。

支持的静态子集：
- `fetch(url, options)` + `JSON.stringify(对象或明确变量)`；
- `axios.post()`、`axios.request()`、`axios({...})` 及有字面量 baseURL 的实例；
- `$.ajax()` / `jQuery.ajax()` 的对象数据、JSON.stringify 数据及明确表单 serialize；
- 字符串常量、字面量配置对象属性、常量拼接、仅含已知常量的模板字符串；
- username/userName/account/mobile/email 等字段和 password/passwd/pwd/pass 等字段；
- 保留已知标量附加参数；动态 CSRF、加密转换、未解析的附加字段不自动猜测。

只读取同源脚本，不跟随脚本重定向；默认最多 8 个外部资源、单个 2 MiB、
总读取预算 8 MiB，静态依赖深度 1。支持 modulepreload 与字面量模块依赖，
同一个解析器内缓存资源；带自定义或动态请求头的通用请求暂交由配置式适配，避免遗漏头部。
首页只跟随一个明确的同源登录链接，不进行路径枚举。
资源限制可在 `parserConfig` 中调整。此功能不是 JavaScript 执行器。

若规则无法确定接口，在 `conf/config.py` 的 `parserConfig["site_profiles"]` 中添加：

```python
{
    "page_url": "http://127.0.0.1:9000/login",  # 精确匹配，不使用通配域名
    "endpoint": "/api/session",              # 必须同源
    "encoding": "json",                     # json 或 form，当前仅 POST
    "username_field": "account",
    "password_field": "secret",
    "extra_data": {"tenant": "demo"},        # 不在这里存储用户名或密码
    "headers": {},                          # 可选的明确静态请求头
    "success_fields": {"result.success": True},
    "required_token_fields": ["result.accessToken"],
    "token_fields": [],
}
```

`success_fields` 使用精确类型比较，并且所有字段同时匹配；缺失字段不等于 null。
`required_token_fields` 中的路径必须返回非空字符串。若仅需要 Token 判定，
可省略 success_fields，并设置 `token_fields: ["data.token"]`。
配置式成功规则不使用全局成功字段和默认 Token 字段作为后备。
全局 `json_success_fields/json_token_fields` 也支持点分隔的嵌套对象路径。
找到接口不等于证明身份或权限有效；仅有长度变化、跳转或 Cookie 变化仍不报成功。

典型诊断：`AMBIGUOUS_INTERFACE`（接口/字段映射不唯一）、`AMBIGUOUS_ENTRY`
（入口不唯一）、`UNRESOLVED_FIELDS`（字段未确定）、`CAPTCHA_REQUIRED`、
`NO_LOGIN_INTERFACE`（缺少表单或静态接口）、`INVALID_PROFILE`、`PAGE_HTTP_ERROR`。
候选及来源证据保存在 `Parser.candidates`，资源读取警告保存在 `resource_warnings`。

运行回归测试（只使用离线样例与临时回环 HTTP 服务）：

```bash
python3 -m unittest discover -s checks -p '*regression.py' -v
python3 -m checks.local_integration
```


## URL 与登录请求并发

`-t/--threads` 控制同时检查的 URL 数（默认 5）；`-c/--concurrency`
控制每个 URL 内的候选登录请求并发数（默认 5，也可配置
`crackConfig["concurrency"]`）。即使仅有一个 URL，候选请求也会并发：

```bash
python3 webcrack.py -u http://127.0.0.1:8000/login -t 1 -c 5
python3 webcrack.py -f url.txt -t 2 -c 3 -o results/output.txt
python3 webcrack.py -f url.txt -t 1 -c 1  # 完全串行
```

JSON 接口识别结果在候选阶段复用，不重复下载/解析 JS；每个工作线程
独立初始化页面 Cookie 并复用自己的 HTTP 连接。请求参数及成功规则独立复制，
不跨线程共享会话。HTML/jQuery 表单仍逐候选刷新 Cookie、隐藏字段和验证码状态，但接口映射、
脚本识别结果与成功规则复用；不是只隐藏重复日志。失败基线先顺序完成，候选请求随后并发，成功候选
再通过独立会话顺序复核。限流、锁定、异常或复核成功时停止提交新候选，
取消尚未执行的候选；已经发出的请求会收尾，受请求超时约束。失败复核后
继续处理剩余候选。若多个凭据都有效，返回先完成复核的凭据。

待处理候选数量不超过 `concurrency`，不一次创建整个字典的 Future。
`--delay` 是各请求后的等待间隔，不是全局限速。候选阶段的请求并发上限
约为 `threads × concurrency`，复核期间还可能多一个请求/URL；总资源量需
同时考虑这两个参数。URL 汇总仍保持输入顺序，日志 ID 使用线程局部存储。

当前 HTTP 客户端是同步 `requests`，因此使用有界线程池执行请求。
`asyncio.to_thread(requests...)` 仍使用线程；真正异步化需要迁移 HTTP 客户端、
页面解析器的网络调用和延迟等待，不能只在登录方法外加 `async def`。
高并发时异步客户端可能降低线程资源开销，具体吞吐仍取决于网络延迟、
连接数、页面重载成本与服务端限制。


Axios、jQuery、静态接口、CMS 和配置式接口在同一 URL 任务内复用识别结果。
成功复核仍使用独立新会话并刷新页面状态，结构未变时不重复识别。页面 URL、
表单结构或脚本引用/内联内容变化时明确记录原因并重新识别；缓存不跨任务或运行。
已发现的登录入口直接刷新，不反复从首页寻找入口。
JSON 工作线程复用失败请求后的 Cookie；依赖每次请求刷新动态令牌的接口
需要按实际页面机制实现令牌刷新流程，当前静态识别不覆盖这种动态机制。

输入列表中完全相同的 URL 会去重并保留首次出现的顺序；不同查询参数的 URL 不合并。

## 原版资料与历史

原版作者：[@yzddmr6](https://github.com/yzddmr6)。
原版开发文档：[WebCrack 发布说明](https://yzddmr6.com/posts/webcrack-release/)。
以下为保留的原版历史日志，不代表 WebCrack-plus 的版本发布记录。

### 2021/07/27 `v(2.2)`

* 修复后台页面没有action字段导致的解析问题

* 配置文件字典改为加载txt方式

### 2021/03/15 `v(2.1)`

* 修复目标为IP时字典生成失败的BUG

### 2021/02/22 `v(2.0)`

* 代码重构，解耦，面向对象

* `conf/config.py`中可以自定义全局参数

* 去掉预请求，优化核心判断逻辑

* 修复部分BUG

### 2020/02/25 `v(1.1)`

代码准备全部重构，先发一个修复BUG的临时版本

* 优化核心判断逻辑

* 修复两处表单识别问题

* 增加黑名单关键字

### 2019/09/09 `v(1.0)`

* 项目开源
