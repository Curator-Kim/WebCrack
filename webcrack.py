import argparse
import csv
from pathlib import Path
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
    results = []
    for url in url_list:
        result = CrackTask().run(cur_num, url)
        if isinstance(result, dict):
            results.append(result)
        cur_num += 1
    return results


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
        urls = [target]
    else:
        try:
            with open(target, encoding="utf-8") as handle:
                urls = [line.strip() for line in handle]
        except OSError as exc:
            parser.error(str(exc))
        urls = [url for url in urls if url and not url.startswith('#') and
                not any(domain in url for domain in conf.config.IGNORE_DOMAINS)]

    output = Path(args.output or conf.config.logConfig["output_filename"])
    # 在任务开始前检查路径；不提前清空已有文件。
    if output.exists() and not output.is_file():
        parser.error(f"输出路径不是文件: {output}")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.resolve() == Path(args.file or target).resolve() and not '://' in target:
            parser.error("输出文件与输入列表文件必须不同")
    except OSError as exc:
        parser.error(f"准备输出目录失败: {exc}")
    start = datetime.datetime.now()
    results = single_process_crack(urls)
    try:
        write_results(output, results)
    except OSError as exc:
        parser.error(f"写入成功结果失败: {exc}")
    print(f'All processes done! Cost time: {datetime.datetime.now() - start}')
    print(f'成功结果: {len(results)} 条，已保存至 {output.resolve()}')
    return 0


def write_results(path, results):
    """UTF-8 TSV：表头加每行一条通过复核的结果。"""
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["url", "username", "password"], delimiter="\t")
        writer.writeheader()
        writer.writerows(results)



if __name__ == '__main__':
    raise SystemExit(main())
