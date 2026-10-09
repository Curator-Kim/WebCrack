"""离线并发回归；只使用模拟任务和临时文件。"""
import contextlib
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import logs.log as Log
import webcrack


class ThreadingTests(unittest.TestCase):
    def test_concurrent_tasks_isolated_and_results_ordered(self):
        barrier = threading.Barrier(3)
        instances = []
        mutex = threading.Lock()

        class Task:
            def __init__(self):
                with mutex:
                    instances.append(self)

            def run(self, task_id, url):
                self.url = url
                barrier.wait(timeout=5)  # 顺序执行会超时，三个任务必须真正同时运行。
                return {'url': self.url, 'username': str(task_id), 'password': 'fixture'}

        with patch.object(webcrack, 'CrackTask', Task), contextlib.redirect_stdout(io.StringIO()):
            results = webcrack.multi_thread_crack(['a', 'b', 'c'], threads=3)
        self.assertEqual([r['url'] for r in results], ['a', 'b', 'c'])
        self.assertEqual([r['username'] for r in results], ['1', '2', '3'])
        self.assertEqual(len({id(task) for task in instances}), 3)

    def test_out_of_order_completion(self):
        later_finished = threading.Event()

        class Task:
            def run(self, task_id, url):
                if task_id == 1:
                    if not later_finished.wait(5):
                        raise AssertionError('second worker did not run')
                else:
                    later_finished.set()
                return {'url': url, 'username': 'fixture', 'password': 'fixture'}

        with patch.object(webcrack, 'CrackTask', Task), contextlib.redirect_stdout(io.StringIO()):
            results = webcrack.multi_thread_crack(['a', 'b'], 2)
        self.assertTrue(later_finished.is_set())
        self.assertEqual([r['url'] for r in results], ['a', 'b'])

    def test_one_thread_and_legacy_entry(self):
        seen = []

        class Task:
            def run(self, task_id, url):
                seen.append((task_id, url))

        with patch.object(webcrack, 'CrackTask', Task), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(webcrack.single_process_crack(['a', 'b', 'c']), [])
        self.assertEqual(seen, [(1, 'a'), (2, 'b'), (3, 'c')])

    def test_bounded_submission(self):
        release = threading.Event()
        full = threading.Event()
        yielded = []

        class URLs(list):
            def __iter__(self):
                for url in super().__iter__():
                    yielded.append(url)
                    yield url

        class Task:
            def run(self, task_id, url):
                if task_id == 2:
                    full.set()
                if not release.wait(5):
                    raise AssertionError('workers did not release')

        urls = URLs(range(20))
        with patch.object(webcrack, 'CrackTask', Task), contextlib.redirect_stdout(io.StringIO()):
            with ThreadPoolExecutor(max_workers=1) as coordinator:
                future = coordinator.submit(webcrack.multi_thread_crack, urls, 2)
                try:
                    self.assertTrue(full.wait(5))
                    self.assertEqual(len(yielded), 2)
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=5), [])
        self.assertEqual(len(yielded), 20)

    def test_exception_does_not_drop_other_results(self):
        class Task:
            def run(self, task_id, url):
                if url == 'bad':
                    raise RuntimeError('fixture exception')
                if url == 'empty':
                    return None
                return {'url': url, 'username': 'fixture', 'password': 'fixture'}

        with patch.object(webcrack, 'CrackTask', Task), patch.object(Log, 'Error') as error, \
                contextlib.redirect_stdout(io.StringIO()):
            results = webcrack.multi_thread_crack(['good', 'bad', 'empty', 'last'], 2)
        self.assertEqual([r['url'] for r in results], ['good', 'last'])
        error.assert_called_once()

    def test_empty_and_invalid_threads(self):
        with patch.object(webcrack, 'CrackTask') as task, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(webcrack.multi_thread_crack([], 3), [])
            task.assert_not_called()
        for threads in (0, -1, True, 1.5):
            with self.subTest(threads=threads), self.assertRaises(ValueError):
                webcrack.multi_thread_crack(['a'], threads)

    def test_cli_threads(self):
        parser = webcrack.build_argument_parser()
        self.assertEqual(parser.parse_args([]).threads, 5)
        self.assertEqual(parser.parse_args(['-t', '3']).threads, 3)
        for value in ('0', '-1', '1.5', 'bad'):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                webcrack.main(['-u', 'fixture', '--threads', value])
            self.assertEqual(error.exception.code, 2)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(webcrack, 'multi_thread_crack', return_value=[]) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(webcrack.main(['-u', 'fixture', '-t', '3', '-o', directory + '/out']), 0)
            run.assert_called_once_with(['fixture'], threads=3)

    def test_thread_local_ids_and_complete_log_lines(self):
        barrier = threading.Barrier(4)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'logs.txt'

            def worker(task_id):
                Log.init_log_id(task_id)
                barrier.wait(timeout=5)
                for line in range(25):
                    Log.Error(f'worker={task_id} line={line}')

            with patch.object(Log, 'log_filename', str(path)), contextlib.redirect_stdout(io.StringIO()):
                with ThreadPoolExecutor(max_workers=4) as executor:
                    list(executor.map(worker, range(1, 5)))
            lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 100)
        for task_id in range(1, 5):
            for line in range(25):
                self.assertEqual(sum(f'id: {task_id} worker={task_id} line={line}' ==
                                     text.split('  ', 1)[1] for text in lines), 1)


if __name__ == '__main__':
    unittest.main()
