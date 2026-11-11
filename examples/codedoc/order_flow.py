"""订单流程示例模块：带真实调用链，专门用来演示「代码符号图」第三路检索。

    create_order ──> validate_stock
                 ├─> apply_coupon
                 └─> send_receipt

关系型问题（「create_order 依赖哪些子函数」「谁调用了 apply_coupon」）
纯词面 / 向量检索答不了 —— 答案所在的那条记忆文本里根本没有 `create_order`。
只有图（caller/callee 边）能把它们捞上来，这就是第三路的存在理由。

本文件只参与建图与检索演示，不参与注释写入（codedoc 只改 sample.py 的工作副本）。
"""


def create_order(user, items, coupon=None):
    validate_stock(items)
    total = sum(i["price"] * i["qty"] for i in items)
    if coupon:
        total = apply_coupon(total, coupon)
    send_receipt(user, total)
    return {"user": user, "total": total}


def validate_stock(items):
    for item in items:
        if item.get("qty", 0) <= 0:
            raise ValueError(f"invalid qty: {item}")


def apply_coupon(total, coupon):
    if coupon == "VIP20":
        return total * 0.8
    if total > 1000:
        return total - 100
    raise KeyError(f"unknown coupon: {coupon}")


def send_receipt(user, total):
    print(f"receipt to {user}: {total}")
