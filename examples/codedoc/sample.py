"""被注释 Agent 处理的靶子文件：三个没有任何 docstring 的函数。

codedoc demo 会把本文件复制到工作目录（探针目录，uuid 命名），
由多 Agent 流水线补写 docstring —— 源文件本身永不被直接修改。
"""


def calc_discount(price, level):
    if level == "vip":
        return price * 0.8
    if price > 1000:
        return price - 100
    return price


def send_email(to, subject, body):
    if not to or "@" not in to:
        raise ValueError("invalid address")
    print(f"send to {to}: {subject}")


def parse_config(raw):
    parts = raw.split(";")
    return {k: v for k, v in (p.split("=") for p in parts if "=" in p)}
