import argparse
import csv
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import logs.log as Log
from pathlib import Path
import datetime
import conf.config

from crack.crack_task import CrackTask

author_info = r'''
+---------------------------------------------------+
|                   WebCrack-plus                   |
|        Concurrent Web Login Checking Tool         |
|      Based on WebCrack v2.2 by @yzddmr6            |
+---------------------------------------------------+
'''



def multi_thread_crack(url_list, threads=5):
    """并发执行独立 URL 任务；限制待处理 Future 数并保持结果顺序。"""
    if isinstance(threads, bool) or not isinstance(threads, int) or threads <= 0:
        raise ValueError("threads 必须是正整数")
    print("总任务数: " + str(len(url_list)))
    if not url_list:
        return []
    results = {}
    tasks = iter(enumerate(url_list, 1))

    def run_task(task_id, url):
        # 每个 URL 都创建独立任务及 requests.Session。
        Log.init_log_id(task_id)
        try:
            return CrackTask().run(task_id, url)
        except Exception as exc:
            Log.Error(f"[-] 任务异常: {url}: {exc}")
            return None
        finally:
            Log.init_log_id(None)

    with ThreadPoolExecutor(max_workers=min(threads, len(url_list)),
                            thread_name_prefix="webcrack-plus") as executor:
        pending = {}

        def submit_next():
            task = next(tasks, None)
            if task is not None:
                task_id, url = task
                pending[executor.submit(run_task, task_id, url)] = task_id

        for _ in range(min(threads, len(url_list))):
            submit_next()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                task_id = pending.pop(future)
                result = future.result()
                if isinstance(result, dict):
                    results[task_id] = result
                submit_next()
    return [results[task_id] for task_id in sorted(results)]


def single_process_crack(url_list):
    """保留旧入口；使用一个工作线程顺序执行。"""
    return multi_thread_crack(url_list, threads=1)


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
    if args.threads <= 0:
        parser.error("--threads 必须是正整数")
    if args.concurrency is not None:
        if args.concurrency <= 0:
            parser.error("--concurrency 必须是正整数")
        conf.config.crackConfig["concurrency"] = args.concurrency
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

    unique_urls = list(dict.fromkeys(urls))
    if len(unique_urls) != len(urls):
        print(f"已合并重复 URL: {len(urls) - len(unique_urls)} 条")
    urls = unique_urls

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
    results = multi_thread_crack(urls, threads=args.threads)
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
