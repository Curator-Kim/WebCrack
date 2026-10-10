# 检测机制与配置说明

本文说明当前实现的入口识别、成功判定、会话管理与重试策略。基本用法见 [README](../README.md)。

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

所有静态接口（fetch、字面量 axios、`$.ajax`、`$.post`、jQuery Form Plugin）都会额外扫描
回调中的显式成功条件并登记为响应规则：`if (x.<成功字段>)` → `{字段: true}`、
`if (!x.<成功字段>)` → `{字段: false}`、`if (x.<字段> === <字面量>)` → `{字段: 字面量}`。
成功字段名限定在 `SUCCESS_FIELD_NAMES`（success/authenticated/code/status/... ），
路径相对响应体且最多三段，首段响应变量名会被剥离（`res.data.success` → `data.success`）；
反向比较（`!==` / `!=`）与未知字段名不登记，避免把无关条件当成成功。
jQuery Form Plugin 的 `$("#formId").ajaxSubmit(function (res) { ... })` 同样按静态回调处理，
请求仍提交到表单 `action`。
页面脚本若既无这些条件、也不符合其它静态子集，响应只能凭 `success_words`、
`json_success_fields` 或 `site_profiles` 判定。缺少明确规则时结果记为不确定、不导出，
并在日志中输出 `未获得明确成功规则`；有规则时输出 `成功判定来源: ...` 便于核对。

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

只读取同源脚本，不跟随脚本重定向；默认最多 8 个外部资源、单个 4 MiB、
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
（入口不唯一）、`UNRESOLVED_FIELDS`（字段未确定）、`CAPTCHA_REQUIRED`
（需要验证码但缺少 ddddocr 或定位不到字段/图片）、
`NO_LOGIN_INTERFACE`（缺少表单或静态接口）、`INVALID_PROFILE`、`PAGE_HTTP_ERROR`。
候选及来源证据保存在 `Parser.candidates`，资源读取警告保存在 `resource_warnings`。

运行回归测试（只使用离线样例与临时回环 HTTP 服务）：

```bash
python3 -m unittest discover -s checks -p '*regression.py' -v
python3 -m checks.local_integration
```

## 有限动态脚本发现

默认启用 `parserConfig["discover_dynamic_scripts"]`。除 `<script src>`、
modulepreload 和静态 import 外，也提取 HTML 内联脚本中明确创建的 script
元素的静态 `src`，以及静态数组通过 `forEach` 给 script.src 赋值的路径：

```javascript
const scripts = ['./assets/components.async.js', './assets/umi.js'];
scripts.forEach(src => {
    const script = document.createElement('script');
    script.src = window.utils.getVersionedUrl(src);
    document.body.appendChild(script);
});
```

支持字面量、已知字符串常量、简单常量拼接及 `getVersionedUrl` 包装中的
原始路径；版本包装只提取输入路径，不执行函数或保证查询参数相同。
已下载脚本中的相同加载形式也受 `script_dependency_depth` 限制。
未知函数、运行时网络结果、加密/计算生成的地址以及 Webpack 数字 chunk 映射
不猜测；也不会将页面里的所有 `.js` 字符串都当成资源。

所有新路径仍受同源、禁止跟随重定向、资源数量、单文件大小、总读取预算
和深度限制，并复用任务内资源缓存。默认数量上限仍为 8；资源很多时可按需
调整。增加资源发现不等于支持跨域 API 或运行时 fetch 拦截器。

## V2Board / Umi 登录接口

支持已观察到的 Umi 表单 POST 包装、`/passport/auth/login` 调用与
`data.auth_data` 会话消费组合；不执行 JavaScript，不凭页面标题猜测产品。
当前识别条件并非产品名指纹：同一脚本需要明确的登录调用及 `email/password`
字段、`data.auth_data` 会话消费、POST 与表单编码证据，且 API 前缀可静态确定。
字段顺序不同、载荷对象间接传递、JSON 编码、其他请求包装器、跨脚本调用链、
计算型配置或尚未获取的异步 chunk 仍可能未命中该专用规则。
资源发现上限为 8 个同源脚本、1 层静态依赖、单文件 4 MiB、总计 8 MiB。
修复了压缩 JS 内正则字面量中的引号/斜杠被误读为字符串/注释的问题；
静态子集解析仍不等同于完整 JavaScript 运行时。
自动映射 `email` / `password`，以 `application/x-www-form-urlencoded`
提交，使用 `Accept: application/json`。用户名列表需要填入邮箱，默认 `admin`
并非有效邮箱。登录成功需非空字符串 `data.auth_data`，仅有订阅 `data.token`
不算登录成功；仍执行失败基线与独立会话复核。

适配器读取明确的 `window.settings.host`、生产构建的 `/api/v1` 前缀，以及
此主题 `defaultConfig.apiUrl` fetch 重写。缺失、动态或冲突配置报告
`V2BOARD_CONFIG_UNRESOLVED` / `AMBIGUOUS_INTERFACE`，可通过既有 `site_profiles`
明确配置其他构建。同源接口自动启用；跨域接口必须匹配明确源或 API 主机模式，
其余报告 `V2BOARD_CROSS_ORIGIN`，不访问后端。配置位于 `conf/config.py`：

```python
parserConfig["v2board_api_origins"] = ["https://backend.example.test"]
parserConfig["v2board_api_origin_patterns"] = ["https://api.*.*"]
```

明确源按 scheme/host/port 匹配；默认 API 模式匹配 `https://api.example.test`
及 `https://api.eu.example.test` 等主机，默认端口仅 443。需要其他端口时明确配置
`https://api.*.*:8443`；HTTP 需单独配置 `http://api.*.*`。不是对整个 URL 做任意 glob。
地址仍来自前端声明的 `settings.host` 或有效的 `defaultConfig.apiUrl` 重写，
不会猜测、替换成固定域名或扫描其他域名；没有可确定的配置时仍报告诊断。
只有匹配现有 V2Board/Umi 登录证据的构建使用该模式，不声称覆盖所有主题或分支。
设置 `v2board_api_origin_patterns=[]` 可恢复只按明确源匹配。
这不改变通用解析器及 `site_profiles` 的同源约束。
默认明确源列表为空；其他非 `api.<域名>` 后端可按需加入明确源列表。
账户密码与会话 Token 不写入此配置。
HTTP 500 的明确邮箱/密码错误作为失败，不反复重试；参数校验错误作为失败，
密码次数限制及 HTTP 429 停止当前任务；未知 5xx 继续按既有服务端异常逻辑处理。
不绕过后台锁定或限流。脚本单文件上限提高至 4 MiB，总上限仍为 8 MiB。

```bash
python3 -m unittest checks.v2board_regression -v
```

## 验证码识别（可选 ddddocr）

登录页检测到验证码时，只有在当前 Python 环境可导入 ddddocr 且模型可用的情况下
才自动识别；否则沿用原行为，直接放弃该站点（诊断码 `CAPTCHA_REQUIRED`）。
通过 `--no-captcha` 可显式关闭识别，同样跳过需要验证码的站点。

```bash
python3 -m pip install ddddocr        # 可选依赖，安装后自动启用
python3 webcrack.py -f url.txt        # 检测到验证码时自动识别并注入
python3 webcrack.py -f url.txt --no-captcha   # 关闭识别，验证码站点跳过
```

检测与求解流程：

1. 按 `parserConfig["captcha_keyword_list"]` 在表单文本与标识属性中判定是否需要验证码；
2. 依 `captchaConfig["field_keyword_list"]`（强）与 `weak_field_keyword_list`（弱，如 `code`）
   定位验证码输入字段，按 `image_keyword_list` 或表单内唯一图片定位图片地址；
3. 每次登录提交前重新请求验证码图片（相对地址按页面地址解析，支持内联 `data:` URI），
   串行调用 ddddocr 识别，并按 `min_length`/`max_length` 过滤结果；
4. 若响应命中 `captcha_fail_words`（如“验证码错误”），重新获取并识别后按
   `request_retries` 重发，避免把验证码失败误判为密码失败；
5. `solve_retries` 次仍识别失败时放弃该站点，不将不确定结果导出。

相关配置位于 `conf/config.py` 的 `captchaConfig`。配置式接口（`site_profiles`）可用
`captcha` 键显式声明字段与图片地址，例如：

```python
{"page_url": "https://example.test/login", "endpoint": "/session",
 "username_field": "username", "password_field": "password",
 "success_fields": {"authenticated": True},
 "captcha": {"field": "captcha", "image_url": "/captcha.png"}}
```

OCR 识别存在误差：站点返回的错误提示若不包含验证码关键字，仍可能被归类为密码失败。
识别能力来自第三方 `ddddocr`，其结果不参与成功判定，成功仍以明确证据和独立会话复核为准。

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
不跨线程共享会话。并发模式下，HTML/jQuery 表单逐候选刷新 Cookie、隐藏字段和验证码状态，但接口映射、
脚本识别结果与成功规则复用；不是只隐藏重复日志。串行模式直接复用主 Session 和现有请求计划，不逐候选刷新表单；
依赖一次性隐藏字段的页面需特别注意这一差异。失败基线先顺序完成，候选请求随后并发，成功候选
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

## 候选生成与异常处理

入口识别成功后，先用配置中的测试凭据提交两次，建立失败基线；基线出现成功证据、
限流或异常状态时停止当前站点。普通候选来自基础账号/密码列表与域名派生密码，
密码中的 `{user}` 替换为当前用户名。普通字典未命中且任务未停止时，按 CMS 或全局配置
决定是否进入万能密码检测。

登录响应为未知 5xx 时默认额外重试 2 次（`server_error_retries`），每次重试重新获取
并识别所需验证码。重试后仍为 5xx 则跳过当前候选；连续 3 个候选仅得到服务端错误
（`server_error_limit`）才停止站点。正常候选响应会重置连续计数。
429、锁定提示及其他需停止的错误不继续提交候选；失败基线和复核阶段的异常直接停止任务。
验证码输入框声明 `maxlength` 或 `data-length` 时按声明长度过滤识别结果，否则使用
`min_length` / `max_length`。

## 超时与重试

页面 GET、同源登录入口 GET、JS 文件 GET（含流式响应体读取）以及所有登录
POST（含失败基线和成功复核）统一使用超时重试。默认首次失败后额外重试
**3 次，最多 4 次尝试**；每次重试前等待 0.3 秒，每轮使用现有 `--timeout`。
这不是整体截止时间，多轮重试会增加总耗时。

```python
crackConfig["timeout_retries"] = 3       # 0 可关闭超时重试
crackConfig["timeout_retry_delay"] = 0.3
```

只重试连接/读取超时及被 requests 包装为 ConnectionError 的响应体读取超时。
普通连接失败、TLS 错误、401、429、其他 HTTP 错误、验证码或登录失败不因该
机制重试。任务停止后取消尚未发起的重试；已发出的请求仍需等待收尾。
JS 下载超时重试时丢弃部分内容并关闭响应，成功后才缓存完整脚本。

POST 读取超时不代表服务端没有处理请求；重发相同凭据可能重复计入失败次数
或触发锁定。本机制不保证登录请求幂等，请结合目标限制调整重试和并发数。

## 成功结果导出

运行结束后，将通过复核的成功结果写入 UTF-8 制表符分隔文件，包含
`url`、`username`、`password` 三列。默认文件为当前目录的 `output.txt`，
可通过 `-o/--output` 指定路径，也可修改 `conf/config.py` 的
`logConfig["output_filename"]`。每次运行覆盖输出文件；没有成功结果时仅写表头。
原有日期日志保持不变。结果包含明文凭据，请妥善保管。

```bash
python3 webcrack.py -f url.txt -o results/output.txt
```

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
