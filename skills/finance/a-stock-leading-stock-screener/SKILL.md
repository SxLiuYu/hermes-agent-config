---
name: a-stock-leading-stock-screener
title: A股龙头股实时筛选CLI
description: 基于Sina API的A股实时行情CLI工具，支持异步获取行情数据、龙头股筛选（涨幅≥9.5%、成交额≥1亿、换手率≥5%）、JSON/Table输出
category: finance
version: 1.0
depends_on: ["sina_scraper.py", "stock_filter.py", "stock_cli.py"]
---

# A股龙头股实时筛选 CLI

> 用 Sina Finance API 实时捕捉龙头股特征

## 触发条件
- "筛选龙头股"、"今天有什么涨停"、"龙头股"、"打板"、"强势股筛选"
- 用户提到 stock_cli 或 sina_scraper

## 架构

```
Sina API (hq.sinajs.cn)
    ↓ aiohttp 异步批量请求
sina_scraper.py (解析→dict)
    ↓
stock_filter.py (龙头特征筛选)
    ↓
stock_cli.py (CLI入口 → JSON / Table输出)
```

## 工具位置

| 文件 | 路径 | 用途 |
|------|------|------|
| sina_scraper.py | ~/.hermes/tools/sina_scraper.py | 异步HTTP客户端，解析Sina行情数据 |
| stock_filter.py | ~/.hermes/tools/stock_filter.py | 龙头股筛选器 |
| stock_cli.py | ~/.hermes/tools/stock_cli.py | CLI入口（argparse） |
| requirements.txt | ~/.hermes/tools/requirements.txt | aiohttp>=3.9.0 |

## 使用方法

```bash
# 查看帮助
python3 ~/.hermes/tools/stock_cli.py --help

# 查询指定股票实时行情（JSON）
python3 ~/.hermes/tools/stock_cli.py --stock sh600000,sz000001 --output json

# 筛选龙头股（表格输出）
python3 ~/.hermes/tools/stock_cli.py --filter

# 监控默认龙头池
python3 ~/.hermes/tools/stock_cli.py --watch
```

## 龙头股筛选条件

| 条件 | 阈值 | 说明 |
|------|------|------|
| 涨幅 | ≥ 9.5% | 接近涨停板 |
| 成交额 | ≥ 1亿 | 资金活跃 |
| 换手率 | ≥ 5% | 筹码充分交换 |

## 股票代码格式

- 上海: `sh600000` (浦发银行)
- 深圳: `sz000001` (平安银行)
- 科创板: `sh688xxx`
- 创业板: `sz300xxx`

## 注意事项

1. **Sina API 限制**：必须在交易时段（9:30-15:00）内调用，盘后数据可能为空
2. **代码前缀**：sh=上海，sz=深圳，必须小写
3. **异步并发**：批量查询时自动控制并发数，避免被Sina限流
4. **停牌处理**：停牌股票会返回空数据或旧数据，筛选时自动过滤
