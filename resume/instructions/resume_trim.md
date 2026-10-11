# Resume Trim Instructions

## Purpose

This instruction is used **only when Python has already rendered the resume to PDF and confirmed that the PDF is longer than one page**.

Its job is to reduce the resume content by the smallest amount necessary to make the final PDF fit on one page while preserving the strongest evidence for the target job.

This stage does **not** create HTML, render PDF, count pages, or change layout. Python remains responsible for all rendering and validation.

---

## Trigger

Run this instruction only when:

```text
pdf_validation.page_count > 1
```

If the PDF is already one page, this instruction must not be called.

---

## Responsibility Boundary

### Python is responsible for

- Reading the job JSON/state.
- Passing the required JSON objects into this instruction.
- Filling the fixed resume HTML template.
- Rendering HTML to PDF.
- Counting PDF pages.
- Measuring the actual rendered overflow.
- Calling this trim step when the PDF exceeds one page.
- Validating the returned JSON schema.
- Regenerating HTML and PDF from the trimmed resume.
- Re-running PDF validation after every trim pass.

### This instruction is responsible for

- Reading the current `resume_v2` content.
- Reading the target-job / quality review evidence supplied by Python.
- Reading the PDF overflow measurement supplied by Python.
- Identifying the lowest-value content that can be shortened or removed.
- Returning a shorter resume using the **same JSON schema as `resume_v2`**.

### This instruction must never

- Modify HTML.
- Modify CSS.
- Change fonts, margins, line-height, spacing, page size, or scaling.
- Create or render a PDF.
- Guess whether the resume now fits on one page.
- Add new achievements, metrics, skills, technologies, responsibilities, or facts.
- Expand content while trying to shorten another section.
- Rewrite the resume from scratch.

---

## Required Inputs

Python should provide the following JSON objects.

### 1. `resume_v2`

The current resume content that produced the oversized PDF.

Expected structure:

```json
{
  "job_title": "Junior Resource Strategy Analyst",
  "summary": "...",
  "qualifications": [
    "...",
    "...",
    "..."
  ],
  "skills": {
    "technical": [
      "Python",
      "SQL",
      "Power BI"
    ],
    "professional": [
      "Stakeholder Engagement",
      "Data Storytelling"
    ]
  },
  "experience": [
    {
      "role": "<experience[0].role>",
      "company": "<experience[0].company>",
      "date": "<experience[0].date>",
      "bullets": [
        "...",
        "...",
        "..."
      ]
    }
  ],
  "project": {
    "name": "...",
    "tools": "...",
    "date": "...",
    "bullet": "..."
  },
  "education": [
    {
      "degree": "<education[0].degree>",
      "school": "<education[0].school>",
      "details": "<education[0].details>",
      "date": "<education[0].date>",
      "coursework": "<education[0].coursework>"
    }
  ]
}
```

`<...>` 是 Python 提供的实际值（来自候选人的 `candidate.json`）；示例只展示第 1 条。

This is the same schema as the `instructions/resume_v2.md` Output Contract. Use the actual schema supplied by Python. Do not add, remove, or rename fields, and do not change field types (for example, `project` is one object and `project.bullet` is one string).

### 2. `quality_feedback`

The current structured ATS quality-review feedback for the target role.

Use it only to determine which keywords, skills, qualifications, and evidence must be protected during trimming.

Do not add a keyword merely because it appears in `quality_feedback`. This stage is trimming only.

### 3. `pdf_validation`

Python must provide a quantitative description of the overflow.

Recommended structure:

```json
{
  "page_count": 2,
  "overflow_pages": [2],
  "overflow_lines": 3,
  "overflow_lines_by_page": {
    "2": 3
  },
  "overflow_text": [
    "Relevant coursework: Data Management, Statistical Learning, Data Visualization, Optimization",
    "Bachelor of Science in Statistics, Riverside Institute of Technology",
    "Sep 2019 – Jun 2023"
  ],
  "trim_iteration": 1
}
```

`overflow_lines` must represent the number of **visually rendered PDF text lines on pages after page 1**, not the number of JSON bullets and not the number of source-code lines.

`overflow_text` should contain the extracted visible lines from the overflow page(s). It is diagnostic context only; do not simply delete whatever happens to appear on page 2.

---

## Trim Target

For each trim pass, calculate:

```text
target_reduction_lines = overflow_lines + 1
```

The extra line is a small safety buffer for normal text wrapping differences.

Example:

```text
overflow_lines = 3
 target_reduction_lines = 4
```

The goal is to remove approximately four rendered lines of content with the smallest possible loss of job relevance.

This is a target, not proof that the resume will fit. Only Python's next PDF render can determine that.

Do not aggressively shorten the resume far beyond the current target.

---

## Core Principle

Optimize for:

> **the smallest loss of target-job relevance per rendered line removed**

Do not trim from the bottom of the resume simply because the overflow appears there.

Content on page 2 may have been pushed there by content much earlier in the document. Always evaluate the entire resume.

---

## Protected Content

Do not remove or materially weaken content that provides strong evidence for:

- Explicit required skills in the target job.
- High-priority ATS quality-review requirements.
- Direct responsibility matches.
- Strong technical evidence.
- Strong quantified achievements.
- Business impact.
- Required stakeholder, communication, leadership, or collaboration evidence when relevant to the target role.

Also preserve:

- Candidate identity and contact information.
- Target job title.
- Company names.
- Role titles.
- Employment dates.
- Degree names.
- School names.
- Education dates.

A protected keyword may be shortened around, but the relevant evidence itself should remain understandable.

---

## Trim Order

Apply the following stages in order. Stop as soon as the estimated reduction reaches the current `target_reduction_lines`.

### Stage 1 — Remove wording with near-zero information loss

First shorten existing sentences and bullets without deleting the underlying evidence.

Prefer removing:

- Repeated modifiers.
- Redundant phrases.
- Generic filler.
- Repeated descriptions of the same skill.
- Words that cause an otherwise complete line to wrap onto one additional line.

Preserve:

- Action.
- Tool / method.
- Scale or metric.
- Result / business impact.
- Target-job evidence.

Do not replace specific evidence with vague language merely to save space.

### Stage 2 — Trim low-value skills

Trim the skills section before deleting strong experience evidence.

Within `professional` skills, remove first:

- Generic traits.
- Near-synonyms.
- Skills already obvious from stronger resume evidence.
- Skills not meaningfully relevant to the target job.

Within `technical` skills, remove first:

- Low-relevance technologies.
- Duplicative category labels.
- Skills not supported by or useful for the target role.

Protect technical skills explicitly required by the JD / quality-review feedback.

### Stage 3 — Compress Summary and Qualifications

Shorten redundant summary wording first.

For qualifications:

1. Shorten a qualification if the same evidence can remain in fewer words.
2. Remove the weakest or most repetitive qualification only if shortening is insufficient.
3. Normally keep at least **3 qualification bullets**.

Do not remove a qualification that carries a high-priority target-job requirement unless equivalent strong evidence remains elsewhere.

### Stage 4 — Remove low-value education coursework

Relevant coursework (`education[].coursework`) may be shortened by dropping courses, or emptied, when it adds little value for the target role. Do not reword or add courses.

Do not delete or change the degree, school, `details` (GPA), or dates.

### Stage 5 — Shorten low-relevance project content

Project content is not automatically disposable.

If a project directly supports important target-job requirements, protect it.

If the project has low relevance compared with the professional experience, shorten its wording first — a shorter `tools` string and a more compact `bullet` — before deleting strong experience bullets.

The `project` object is a required part of the resume schema: never remove it, never set it to null, and keep its `name` and `date` unchanged.

### Stage 6 — Remove the weakest experience bullet

Use this only when the previous stages cannot reach the target reduction.

For each role:

- Prefer removing the bullet with the lowest target-job relevance.
- Preserve quantified achievements and direct JD matches.
- Preserve the strongest technical and business-impact evidence.
- Normally keep at least **2 bullets per professional role**.

Do not reduce a role below two bullets unless Python invokes another trim pass and no lower-value content remains elsewhere.

---

## Choosing Between Two Possible Cuts

When two edits save a similar amount of space, prefer the edit that:

1. Removes less target-job evidence.
2. Removes less quantified evidence.
3. Removes more generic or duplicated language.
4. Preserves stronger action + method + result structure.
5. Preserves keywords explicitly supported by quality-review feedback.

---

## Multi-Pass Behavior

This instruction performs **one trim pass only**.

After the returned resume is rendered again:

```text
if page_count == 1:
    accept
else:
    Python creates a new pdf_validation object
    Python calls this instruction again with the newly trimmed resume
```

Each new pass must use the latest `resume_v2` and latest PDF validation results.

Do not assume the previous trim was sufficient.

Do not make a large speculative cut merely to avoid another pass.

Python should control the maximum retry count and fail clearly if repeated trimming still does not produce a valid one-page PDF.

---

## Output Rules

Return **only one JSON object**.

The returned object must:

- Use exactly the same schema as the input `resume_v2`.
- Preserve field names.
- Preserve required structural fields.
- Preserve array order unless an item was intentionally removed.
- Contain the complete trimmed resume, not a patch or diff.
- Contain no Markdown fences.
- Contain no commentary.
- Contain no explanation.
- Contain no trim report.

Python will compare the returned object with the previous version if an audit trail is needed.

---

## Final Self-Check Before Returning

Before returning the JSON, verify all of the following:

1. The output is valid JSON.
2. The output uses the same schema as `resume_v2`.
3. No new facts were introduced.
4. No HTML/CSS/layout instructions were changed.
5. No protected required evidence was unnecessarily removed.
6. The edits are primarily deletions or shortening.
7. The estimated reduction is approximately `overflow_lines + 1` rendered lines.
8. The resume was not shortened substantially more than necessary.
9. The full updated resume object is returned.

---

## Pipeline Position

```text
resume_v2 JSON
      ↓
Python fills fixed HTML template
      ↓
Python renders PDF
      ↓
Python PDF validation
      ↓
page_count == 1 ─────────────→ DONE
      ↓ no
build trim payload:
  - resume_v2
  - quality_feedback
  - pdf_validation
      ↓
resume_trim.md
      ↓
trimmed resume_v2 JSON
      ↓
Python validates schema
      ↓
Python regenerates HTML
      ↓
Python regenerates PDF
      ↓
Python PDF validation
      ↓
repeat only if still > 1 page
```
