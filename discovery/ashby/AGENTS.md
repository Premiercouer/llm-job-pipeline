# Ashby Job Pipeline

Stage 01 读取 `config/ashby_board_pool.json`，不扫描或修改 Master、Excluded、Indeterminate 数据。

Prepare：

```bash
python3 scripts/01_search_jobs.py
```

当前 Codex 会话只读取一次 `instructions/title_screening.md` 和
`.runtime/01_title_screen_input.json`，将完整两分区结果写到
`.runtime/01_title_screen_results.json`，然后执行：

```bash
python3 scripts/01_search_jobs.py --phase finalize --ai-invocations 1
```

Title 筛选不在 Python 中使用关键词，不对 cheap filter 前的全部职位调用 AI。
Title 结果不完整或最终输出验证失败时，finalize 必须失败且不得提交 seen state。

Production seen state 是 `.runtime/ashby_seen_jobs.json`，内部 identity 为
`ashby:{board_slug}:{id}`。最终 Job Match 文件严格使用五字段 job schema，且其
`source_key` 与 Greenhouse 一样等于 `apply_url`。

测试必须传 `--limit-boards`，自动切换到隔离的 test seen/checkpoint/artifact。
测试可用 `--max-title-jobs 10` 限制真实 Title 样本；被截断的 eligible jobs 不会写 seen。
测试后不得自动执行 297 Boards production run。
