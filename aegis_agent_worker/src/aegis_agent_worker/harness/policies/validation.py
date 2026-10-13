"""策略构造校验，启动时拒绝非法限制。"""
def positive_limits(instance, names):
    for name in names:
        value = getattr(instance, name)
        if type(value) is not int or value < 1:
            raise ValueError(f"策略字段{name}必须是正整数")
