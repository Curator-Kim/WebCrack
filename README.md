# WebCrack `v(2.2)`

## 工具简介

WebCrack是一款web后台弱口令/万能密码批量检测工具，在工具中导入后台地址即可进行自动化检测。


## 开发文档

https://yzddmr6.com/posts/webcrack-release/

## 更新日志

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

## 工具特点

* 多重判断机制，减少误报

* 随机UA 随机X-Forwarded-For 随机Client-IP

* 可以通过域名生成动态字典

* 可以检测万能密码漏洞

* 支持自定义爆破规则

## 使用方法

下载项目
```
git clone https://github.com/yzddmr6/WebCrack
```

安装依赖
```
pip install -r requirements.txt
```

运行脚本
```
> python3 webcrack.py

+---------------------------------------------------+
| __          __  _      _____                _     |
| \ \        / / | |    / ____|              | |    |
|  \ \  /\  / /__| |__ | |     _ __ __ _  ___| | __ |
|   \ \/  \/ / _ \ '_ \| |    | '__/ _' |/ __| |/ / |
|    \  /\  /  __/ |_) | |____| | | (_| | (__|   <  |
|     \/  \/ \___|_.__/ \_____|_|  \__,_|\___|_|\_\ |
|                                                   |
|                 code by @yzddmr6                  |
|                  version: 2.1                     |
+---------------------------------------------------+

File or Url:

```

输入文件名则进行批量爆破，输入URL则进行单域名爆破。

开始爆破

![image-20210222154621129](README.assets/image-20210222154621129.png)


爆破的结果会保存在`logs/{date}/`文件夹中

![image](https://user-images.githubusercontent.com/46088090/64511693-6a248e80-d317-11e9-9d0c-6114cb194d37.png)


## 自定义配置文件

参数详情见`conf/config.py`文件注释

## 警告！

**请勿用于非法用途！否则自行承担一切后果**

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
JSON 接口可配置 `json_success_fields`（顶层字段，全部严格匹配）。
失败基线也含有的成功标记会被排除；没有明确证据则标记为不确定，不导出。
复核通过独立会话重新获取登录页面及表单状态。无通用规则能证明任意网站已登录，
请按页面实际响应配置明确标记。

## JSON 登录接口识别

支持静态 JavaScript 中的 `fetch('登录路径', {method: 'POST', ...
body: JSON.stringify({username, password})})`，也支持上述字段的显式映射。
读取内联脚本及同源、路径含 auth/login 的外部脚本；不执行 JavaScript，
不跟随脚本重定向；多候选不自动选择。动态计算 URL、axios、嵌套字段等尚未支持。
JSON 请求使用 `application/json`；成功依据配置的 JSON 字段规则，或顶层非空
字符串 `token/access_token`（仅对 JSON 登录模式启用）。401 视为凭据失败，
429 仍停止任务。返回 Token 只证明登录接口接受请求，不自动证明权限有效。

支持当前表单的 `$.post(字面量路径或字面量常量 + 路径,
$("#表单ID").serialize(), 回调)`。请求保持表单编码，脚本引用的接口地址
必须同源且唯一；只在该回调明确使用 `data.code == "200"` 时采纳 JSON 成功码。
不将所有站点的 `code=200` 全局视为登录成功。动态 URL、其他包装器不自动推断。

也会读取同源 `type="module"` 入口（单文件最多 2 MiB，默认最多 4 个）。
支持静态 Axios `create({baseURL: 字面量})`、登录方法 `.post(字面量路径, 参数)`，
以及 `{username: "", password: ""}` 模型到登录方法再到 Token 存储的明确调用链。
若响应拦截器明确用 `code === 200` 返回 `data`，则成功需同时满足数值成功码
和 `data` 非空字符串。仍不执行 JavaScript，不递归解析任意模块或猜测动态参数。

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
