# 数据用量结算

核对双方数据用量，按合同阶梯和周期规则计算账单，支持争议、贷项、费率模拟及独立审批。金额使用精确十进制字符串。

Python 3.11 标准库与 SQLite 即可运行。数据、时钟和文件均在本地，不需要服务凭据。示例只使用合成记录。

## 运行

在仓库根目录执行：

```sh
python -m meterbook replay fixtures/settlement.json
python -m examples.context
python -m unittest discover -s tests -v
python -m compileall -q meterbook examples
```

示例使用内存或临时目录，不会改动持久台账。`examples/context.json` 记录一组可重复导入的业务输入，字段与时间口径见 `examples/数据说明.md`。`examples.context` 检查数据并调用现有产品入口；它只展示原有业务数据，不计算分析结论。

`tests` 覆盖现有业务生命周期、非法输入、版本和完整性边界，另有示例导入回归。完整测试和上述演示可在一次短终端会话中执行。

## 容器

```sh
docker build -t local/meterbook .
docker run --rm --network none local/meterbook
docker run --rm --network none local/meterbook python -m examples.context
```

镜像构建完成后运行和测试不下载文件。持久化使用各自命令的本地路径参数；具体操作及参数可通过 `python -m meterbook --help` 查询。第三方依赖清单为空。
