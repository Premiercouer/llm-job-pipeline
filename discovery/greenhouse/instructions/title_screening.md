# Title Screening

## Task

Based only on the job's `position` (the job title), perform a high-recall broad semantic match to decide whether the job is worth going on to fetch the full JD for.

Do not make inferences based on a nonexistent JD, company background, or other information.

Core principle:

> When uncertain, keep. Only reject jobs that are clearly irrelevant.

## Scope to Keep

Keep jobs that are related, or possibly related, to the following areas:

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
- Other jobs clearly centered on data, analysis, modelling, or research

If the title is fairly vague and the specific responsibilities cannot be determined from the name alone, the job should also be kept.

## Clear Rejections

Jobs that clearly belong to the following types may be rejected:

- Retail / Food Service
- Warehouse / Cleaning / Security
- Trades / Driving / Production Operations
- Clinical Healthcare
- Pure Sales / Account Executive / Business Development
- HR / Recruiting
- Legal
- Accounting / Bookkeeping / Tax
- General Software Development
- DevOps / IT Support / Systems Administration
- General QA Testing
- Marketing Execution / Design / Creative / Content
- Hourly or task-paid AI training gigs

However, if a title of the above types explicitly contains strong data attributes such as ML, AI, Data Platform, or Analytics, do not reject it merely because of its broad job category.

French-primary titles may be rejected directly.

## Input

Receives one job or a batch of jobs. In batch processing, each job has only:

```json
{
  "source_key": "greenhouse:board:123",
  "position": "Business Intelligence Analyst"
}
```

## Output

For the complete batch, strictly return a single classification partition; no reason is needed:

```json
{
  "accepted_ids": ["greenhouse:board:123"],
  "rejected_ids": ["greenhouse:board:456"]
}
```

Requirements:

- Must cover all input `source_key` values
- `accepted_ids` and `rejected_ids` must not overlap and must not contain duplicates
- Must not contain unknown IDs
- Do not output any other fields
- Do not output Markdown
- Do not add explanatory text
