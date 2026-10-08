import argparse
import datetime
import conf.config

from crack.crack_task import CrackTask

author_info = r'''
+---------------------------------------------------+
| __          __  _      _____                _     |
| \ \        / / | |    / ____|              | |    |
|  \ \  /\  / /__| |__ | |     _ __ __ _  ___| | __ |
|   \ \/  \/ / _ \ '_ \| |    | '__/ _' |/ __| |/ / |
|    \  /\  /  __/ |_) | |____| | | (_| | (__|   <  |
|     \/  \/ \___|_.__/ \_____|_|  \__,_|\___|_|\_\ |
|                                                   |
|                 code by @yzddmr6                  |
|                  version: 2.2                     |
+---------------------------------------------------+
'''


def single_process_crack(url_list):
    all_num = len(url_list)
    cur_num = 1
    print("总任务数: " + str(all_num))
    for url in url_list:
        CrackTask().run(cur_num, url)
        cur_num += 1


def build_argument_parser():
    parser = argparse.ArgumentParser(description=conf.config.cliConfig["description"])
    target_group = parser.add_mutually_exclusive_group()
    for definition in conf.config.cliConfig["arguments"]:
        options = definition.copy()
        flags = options.pop("flags")
        group = options.pop("group", None)
        (target_group if group == "target" else parser).add_argument(*flags, **options)
    return parser


def main(argv=None):
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.timeout is not None:
        import math
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            parser.error("--timeout 必须是有限的正数")
        conf.config.crackConfig["timeout"] = args.timeout
    if args.delay is not None:
        import math
        if not math.isfinite(args.delay) or args.delay < 0:
            parser.error("--delay 必须是有限的非负数")
        conf.config.crackConfig["delay"] = args.delay
    if args.proxy:
        from urllib.parse import urlsplit
        proxy = urlsplit(args.proxy)
        if proxy.scheme not in ("http", "https") or not proxy.hostname:
            parser.error("--proxy 需要完整的 HTTP/HTTPS URL")
        conf.config.crackConfig["requests_proxies"] = {"http": args.proxy, "https": args.proxy}
    if args.no_random_headers:
        conf.config.generatorConfig["headers_config"]["enable"] = False

    print(author_info)
    target = args.url or args.file
    if target is None:
        target = input('File or Url:\n').strip()
    if args.url or (not args.file and '://' in target):
        CrackTask().run(1, target)
        return 0
    try:
        with open(target, encoding="utf-8") as handle:
            urls = [line.strip() for line in handle]
    except OSError as exc:
        parser.error(str(exc))
    urls = [url for url in urls if url and not url.startswith('#') and
            not any(domain in url for domain in conf.config.IGNORE_DOMAINS)]
    start = datetime.datetime.now()
    single_process_crack(urls)
    print(f'All processes done! Cost time: {datetime.datetime.now() - start}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
