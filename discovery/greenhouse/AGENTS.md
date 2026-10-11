# Greenhouse Job Pipeline

## 项目用途

本项目用于扫描 Greenhouse boards，发现新的相关职位，并输出下游 Job Match 可以直接读取的标准 jobs JSON。

主要执行脚本：

```text
scripts/01_search_jobs.py
scripts/02_fetch_jd.py
```

Title 筛选规则：

```text
instructions/title_screening.md
```

---

## 执行流程

完整顺序：

```text
01_search_jobs.py
→ 扫描 Greenhouse boards
→ 根据 stable job ID 做 seen 去重
→ unseen ID 暂存在本轮 prepare artifact
→ location 筛选
→ updated_at 最近 7 天筛选
→ 当前 Codex 会话批量做 title 语义宽匹配
→ Python validate/finalize 并一次性提交 seen state
→ 输出通过 title screening 的 candidates

02_fetch_jd.py
→ 读取 Stage 01 candidates
→ 获取 Greenhouse detail / JD
→ 使用 first_published 做最终 7 天校验
→ 清洗 JD
→ 输出 final jobs JSON
```

必须先成功完成 Stage 01，再执行 Stage 02。

---

## 运行前检查

Stage 01 默认读取：

```text
outputs/greenhouse_boards_relevant_merged.json
```

运行 Stage 01 前先确认该 registry 文件存在。

---

## Stage 01

Prepare：

```bash
python scripts/01_search_jobs.py
```

当前 Codex 会话读取 `instructions/title_screening.md` 与 prepare 生成的
`.runtime/01_title_screen_input.json`，将完整分区结果写到
`.runtime/01_title_screen_results.json`，再运行：

```bash
python scripts/01_search_jobs.py --phase finalize
```

Production seen state：

```text
.runtime/greenhouse_seen_jobs.json
```

Stable ID：

```text
greenhouse:{board_token}:{job_id}
```

本轮发现的 unseen ID 在内存和 prepare artifact 中暂存；Title Screen 结果完整性
验证通过、candidate output 验证通过后，再一次 atomic commit 到 seen state。

默认并行参数：

```text
Board workers: --workers 20
Title workers: 无
```

Title Screen 由 orchestrate pipeline 的当前 Codex 会话完成，只读取一次：

```text
instructions/title_screening.md
```

批量输入中的每个对象只有：

```json
{
  "source_key": "...",
  "position": "..."
}
```

Stage 01 完成后会生成 candidate output 和 handoff。

---

## Stage 02

Stage 01 成功后运行：

```bash
python scripts/02_fetch_jd.py
```

Stage 02 根据 Stage 01 handoff 自动读取输入。

主要流程：

```text
title-pass candidates
→ Greenhouse detail API
→ JD
→ first_published <= 7 days
→ final output
```

默认：

```text
--workers 3
```

---

## 日期规则

```text
updated_at
→ Stage 01 的 cheap 7-day filter

first_published
→ Stage 02 的最终 7-day filter
```

最终是否输出职位，以 `first_published` 为准。

---

## Final Output

最终格式：

```json
{
  "date": "YYYY-MM-DD",
  "job_count": 1,
  "jobs": [
    {
      "company": "...",
      "position": "...",
      "jd": "...",
      "apply_url": "...",
      "source_key": "..."
    }
  ]
}
```

输出目录：

```text
outputs/
```

---

## 测试

修改代码后优先执行：

```text
syntax check
→ unit tests
→ 小规模 Stage 01 test
→ Stage 02 test
→ 检查 final output
```

Stage 01 小规模测试示例：

```bash
python scripts/01_search_jobs.py --limit-boards 2
# 当前会话写入 .runtime/01_title_screen_results_test.json
python scripts/01_search_jobs.py --phase finalize --limit-boards 2
```

Test 使用独立 seen state：

```text
.runtime/greenhouse_seen_jobs_test.json
```

测试完成后不要自动继续 production 全量运行。

---

## 重复测试

Test run 会写入：

```text
.runtime/greenhouse_seen_jobs_test.json
```

如果重复使用同一批 boards，之前见过的职位会被直接 seen skip。

如果需要重新从零测试完整链路，可以指定新的 test seen state，例如：

```bash
python scripts/01_search_jobs.py \
  --limit-boards 5 \
  --test-seen-state .runtime/greenhouse_seen_jobs_test_fresh.json
```

---

## 正式运行

正常 production 顺序：

```bash
python scripts/01_search_jobs.py
# 当前会话完成批量 title screen
python scripts/01_search_jobs.py --phase finalize
python scripts/02_fetch_jd.py
```

完成后检查 `outputs/` 中最新生成的 final jobs JSON。
