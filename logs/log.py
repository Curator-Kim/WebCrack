import time, os
import threading
from conf.config import *

date = time.strftime('%Y-%m-%d', time.localtime(time.time()))
log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), date)
os.makedirs(log_dir, exist_ok=True)

success_filename = os.path.join(log_dir, logConfig["success_filename"])
log_filename = os.path.join(log_dir, logConfig["log_filename"])
lock = threading.RLock()
_context = threading.local()


def init_lock(l):
    global lock
    lock = l if l is not None else threading.RLock()


def init_log_id(i):
    _context.task_id = i


def get_time():
    return time.strftime('%Y-%m-%d %X', time.localtime(time.time()))


def write_log(filename, msg):
    with lock:
        with open(filename, "a+", encoding="UTF-8") as log:
            log.write(msg + "\n")


def Info(msg):
    current_time = get_time()
    task_id = getattr(_context, "task_id", None)
    if task_id:
        msg = f"{current_time}  id: {task_id} {str(msg)}"
    else:
        msg = f"{current_time}  {str(msg)}"
    print(msg)


def Error(msg):
    current_time = get_time()
    task_id = getattr(_context, "task_id", None)
    if task_id:
        msg = f"{current_time}  id: {task_id} {str(msg)}"
    else:
        msg = f"{current_time}  {str(msg)}"
    print(msg)
    write_log(log_filename, msg)


def Success(msg):
    current_time = get_time()
    task_id = getattr(_context, "task_id", None)
    if task_id:
        msg = f"{current_time}  id: {task_id} {str(msg)}"
    else:
        msg = f"{current_time}  {str(msg)}"
    print(msg)
    write_log(success_filename, msg)
