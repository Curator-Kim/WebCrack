# WebCrack-plus

基于 [yzddmr6/WebCrack](https://github.com/yzddmr6/WebCrack) v2.2 的增强版本，
用于 Web 登录页面的弱口令及万能密码检测，保留原版字典生成和 CMS 适配能力。
仅在获得明确许可的系统中使用；结果含明文凭据，请妥善保管。

## 相比原版的改动

- **入口识别**：在 HTML 表单和 CMS 识别之外，支持静态 fetch、Axios、jQuery、
  V2Board/Umi 接口、同源登录链接及精确站点配置。
- **两级并发**：分别控制 URL 与单站候选请求并发，隔离工作会话并复用识别结果。
- **更严格的判定**：明确成功证据 + 失败基线排除 + 独立会话复核，不再仅凭响应长度判成功。
- **验证码与重试**：可选 ddddocr 图片识别，增加超时、验证码错误和服务端错误的有界重试。
- **使用与输出**：增加命令行参数、URL 去重、TSV 成功结果汇总及本地回归测试。

不执行页面 JavaScript；前端密码加密、动态令牌等机制不在静态识别能力内。

## 用法

### 安装

```bash
git clone https://github.com/Curator-Kim/WebCrack-plus.git
cd WebCrack-plus
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
# 可选：图片验证码识别
python3 -m pip install ddddocr
```

### 运行

```bash
# 单个登录页面
python3 webcrack.py -u http://127.0.0.1:8000/login

# 批量检测：2 个 URL 并发，每站 3 个候选请求并发
python3 webcrack.py -f url.txt -t 2 -c 3 -o results/output.txt

# 完全串行
python3 webcrack.py -f url.txt -t 1 -c 1

# 交互输入 URL 或文件路径
python3 webcrack.py
```

URL 文件每行一个地址；忽略空行、`#` 注释及配置中的忽略域名，完全相同的 URL 自动去重。

| 参数 | 说明 |
| --- | --- |
| `-u, --url` / `-f, --file` | 单个 URL / URL 文件，二者互斥 |
| `-o, --output` | 成功结果路径，默认 `output.txt`，每次覆盖 |
| `-t, --threads` | URL 并发数，默认 5 |
| `-c, --concurrency` | 单站候选请求并发数，当前配置默认 2 |
| `--timeout` | 请求超时，默认 10 秒 |
| `--delay` | 每次请求后的等待，默认 0.03 秒，非全局限速 |
| `--proxy` | HTTP/HTTPS 代理 URL |
| `--no-random-headers` | 使用默认请求头 |
| `--no-captcha` | 关闭 OCR，跳过需要验证码的站点 |
| `-h, --help` | 查看完整参数 |

配置和账号列表位于 `conf/config.py`，基础密码字典为 `conf/password_list.txt`。
通过复核的结果写入 UTF-8 TSV，列为 `url`、`username`、`password`；没有成功项时仅写表头。
错误和成功日志位于 `logs/<日期>/`。

## 总体运行流程

```text
读取目标、校验参数、去重
    ↓
并发创建独立站点任务
    ↓
识别登录入口、提交字段与成功规则
    ↓
提交两次测试凭据，建立失败基线
    ↓
生成字典并检测候选凭据
    ↓
初判成功 → 独立会话复核 → 通过后记录并结束站点任务
    ↓
未命中且任务未停止 → 按配置继续万能密码检测
    ↓
汇总成功结果并写入文件
```

失败或不确定的候选继续检查；限流、锁定及达到停止条件的异常结束当前站点任务。
只有明确成功证据且复核通过的结果才会导出。

入口识别、成功判定、配置示例、验证码、并发、重试和回归测试详见
[检测机制与配置说明](docs/detection.md)。
