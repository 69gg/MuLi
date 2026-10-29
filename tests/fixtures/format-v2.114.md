# 2.114 原始读取器夹具

来源提交：`43951104bb18a650cfc011cfafe379d5ffc2c5f2`。

以下文件按该签名提交的 Git blob 原字节复制，用于旧读取器合同回归。
这里冻结源码，不代表安装、启动或验证过旧版发行包。

完整旧格式为 database=1、objects=3、configuration=2、ingress=4、
outbox=3、memory=1、install_profile=1。

| 路径 | SHA-256 |
| --- | --- |
| `tests/fixtures/format_admission_v2_114.py` | `f97071025ab5547f72a437c86075f684595cd451c5d5e504d966d4823d0bff24` |
| `tests/fixtures/attachment_memory_source_v2_114.py` | `116615e7ea720cd7b600935574a5db6d421c9365e40c96f661fd2dc7ddc3fd72` |
| `tests/fixtures/compatibility_v2_114.json` | `8abaa81d42029906be671f5d3dea2ac370fb9031978acbe1abf35d05bebab1a7` |

保留这些原字节和已有 2.112 夹具，不随现行读取器更新。
