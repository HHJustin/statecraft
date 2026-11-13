"""订单处理模块 —— 无缺陷对照文件。

功能与 target.py 一一对应，但每类缺陷都已修正。用途只有一个：
**验证审查 Agent 不会对着干净代码编问题**。误报率是审查系统可用性的
底线——一个到处报"可能有问题"的审查者，很快就没人看了。

对应关系：
  average()       空列表显式抛 ValueError，而不是让 ZeroDivisionError 冒出来
  send_receipt()  只捕获具体异常并转成带上下文的错误
  process()       参数名说明语义（amount / tax_rate）
  load_config()   用 with 保证句柄释放
  register()      bucket=None 哨兵，避免可变默认参数共享
"""

import json
from decimal import Decimal, ROUND_HALF_UP

DEFAULT_CHANNEL = "email"


def average(values):
    """返回列表平均值；空列表抛 ValueError。"""
    if not values:
        raise ValueError("values 不能为空")
    return sum(values) / len(values)


def format_money(cents):
    """分转元，保留两位小数；用 Decimal 量化避免二进制浮点误差。"""
    return str((Decimal(cents) / 100).quantize(Decimal("0.01"),
                                               rounding=ROUND_HALF_UP))


def send_receipt(channel, payload):
    """发送回执；不支持的渠道抛 ValueError。"""
    if channel != DEFAULT_CHANNEL:
        raise ValueError(f"不支持的渠道: {channel}")
    return f"sent:{payload}"


def process(amount, tax_rate):
    """按税率计算含税金额；税率为负抛 ValueError。"""
    if tax_rate < 0:
        raise ValueError("税率不能为负")
    return amount * (1 + tax_rate)


def load_config(path):
    """读取 JSON 配置文件；句柄由 with 保证释放。"""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def register(item, bucket=None):
    """把条目登记到桶里；bucket 缺省时新建，避免跨调用共享。"""
    if bucket is None:
        bucket = []
    bucket.append(item)
    return bucket
