# Python Rules

本文件在编写或修改 Python 时读取。

---

# 1. import

正常依赖直接 import。

不要用宽泛 `try-except` 隐藏必需依赖缺失。

只有产品本身明确支持 optional dependency 时，才允许针对该 optional dependency 做受控处理，并需要对应测试。

---

# 2. 异常处理

优先 fast-fail。

只捕获能够在当前层正确处理的异常。

不得：

* `except Exception: pass`
* 吞掉错误后返回成功
* 为了测试通过屏蔽异常
* 用过宽异常捕获掩盖编程错误

---

# 3. identifier 与注释

identifier 保持英文原名。

代码注释使用中文时，应解释设计意图、边界条件或不直观行为。

不要逐行翻译代码。

---

# 4. 类型与数据边界

已有类型系统、schema 或 validation library 时优先复用。

不要用大量隐式默认值掩盖缺失字段。

外部输入在边界处验证，内部代码避免重复无意义校验。

---

# 5. 测试

Python 修改必须同时遵守 `.agent-rules/testing.md`。

使用 `unittest.mock`、`pytest monkeypatch`、Fake repository 等替身时，必须说明验证目标和证据边界。
