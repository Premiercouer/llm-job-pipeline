# Title Screening

## 任务

只根据职位 `position`（职位 title）做高召回语义宽匹配，判断该职位是否值得继续获取完整 JD。

不要根据不存在的 JD、公司背景或其他信息进行推断。

核心原则：

> 不确定时保留。只有明显无关的职位才拒绝。

## 保留范围

保留与以下方向相关或可能相关的职位：

- Data / Analytics / BI / Reporting / Insights
- Business Analyst / Operations Analyst
- Data Engineering / ETL / Data Platform / Data Quality / Governance
- Data Science / Statistics / Modelling
- Machine Learning / AI / NLP / Computer Vision
- Research / Market Research / Competitive Intelligence
- Risk / Fraud / AML / Sanctions / Credit / Financial Analysis
- Strategy / Planning / Pricing / Economics / Forecasting
- RevOps / GTM Planning / Sales Effectiveness
- Process / Performance Improvement
- 其他明显以数据、分析、建模或研究为核心的岗位

如果 title 比较模糊，仅凭名称无法判断具体职责，也应保留。

## 明确拒绝

可以拒绝明显属于以下类型的职位：

- Retail / Food Service
- Warehouse / Cleaning / Security
- Trades / Driving / Production Operations
- Clinical Healthcare
- 纯 Sales / Account Executive / Business Development
- HR / Recruiting
- Legal
- Accounting / Bookkeeping / Tax
- 普通 Software Development
- DevOps / IT Support / Systems Administration
- 普通 QA Testing
- Marketing Execution / Design / Creative / Content
- 小时制或 task-paid AI training gigs

但是，如果上述 title 明确包含 ML、AI、Data Platform、Analytics 等强数据属性，则不要仅因为岗位大类而拒绝。

French-primary title 可直接拒绝。

## 输入

接收一个或一批职位。批量处理时，每个职位只有：

```json
{
  "source_key": "greenhouse:board:123",
  "position": "Business Intelligence Analyst"
}
```

## 输出

完整批次严格返回一次分类分区，不需要 reason：

```json
{
  "accepted_ids": ["greenhouse:board:123"],
  "rejected_ids": ["greenhouse:board:456"]
}
```

要求：

- 必须覆盖输入的全部 `source_key`
- `accepted_ids` 与 `rejected_ids` 不得重叠、不得重复
- 不得包含未知 ID
- 不输出其他字段
- 不输出 Markdown
- 不补充解释文字
