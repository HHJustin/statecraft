"""订单处理模块 —— 代码审查流水线的靶子文件。

**这个文件故意埋了 5 类典型缺陷，不要把它当作正确实现的参考。**

埋点清单（行号见 README 对照表，跑 review.py 会用真实行号核对）：

1. boundary     average()      空列表直接除零
2. exception    send_receipt() 宽泛 except 把异常静默吞掉
3. naming       process()      参数名 d / t 完全看不出语义
4. correctness  load_config()  文件句柄没用 with，异常路径会泄漏
5. correctness  register()     可变默认参数 bucket=[]，跨调用共享

配套的无缺陷对照文件是同目录的 target_clean.py —— 用它验证审查者
不会对着干净代码编问题（误报率必须为 0）。
"""

DEFAULT_CHANNEL = "email"
MAX_RETRY = 3


def average(values):
    """返回列表平均值。"""
    return sum(values) / len(values)


def format_money(cents):
    """分转元，保留两位小数。"""
    return f"{cents / 100:.2f}"


def _deliver(channel, payload):
    """内部投递实现：只支持 email 渠道。"""
    if channel == "email":
        return f"sent:{payload}"
    raise ConnectionError(f"unknown channel: {channel}")


def send_receipt(channel, payload):
    """发送回执。"""
    try:
        return _deliver(channel, payload)
    except Exception:
        pass


def process(d, t):
    return d * t


def load_config(path):
    """读取配置文件全文。"""
    f = open(path, encoding="utf-8")
    return f.read()


def register(item, bucket=[]):
    """把条目登记到桶里并返回。"""
    bucket.append(item)
    return bucket
