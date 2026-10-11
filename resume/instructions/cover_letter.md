# Cover Letter Generation Instructions

## Purpose

This file defines how the Cover Letter Worker should generate a tailored cover letter for a single job.

The Cover Letter Worker is responsible only for producing the cover-letter body content as structured JSON.

It does **not**:

- control the browser,
- decide whether a cover letter is needed,
- modify the resume,
- modify the job description,
- generate HTML directly,
- generate PDF directly,
- manage application state,
- create handoffs,
- create result manifests,
- submit an application.

The Browser/Application flow decides whether a cover letter should be generated.

Typical trigger conditions include:

- the application page provides a dedicated Cover Letter upload field;
- the application page allows an additional supporting document beyond the resume.

Once triggered, Python identifies the current job, loads the required job data, invokes this worker, validates the returned JSON, inserts the content into the fixed HTML template, and converts the HTML to PDF.

---

## Inputs

The worker receives one JSON object prepared by Python.

The worker receives exactly this structure:

```json
{
  "job": {
    "position": "...",
    "company": "...",
    "location": "...",
    "jd": "..."
  },
  "candidate": {
    "resume_v1": "complete plain-text Resume V1",
    "resume_v2": {},
    "education_status": [{"school": "...", "degree": "...", "date": "...", "status": "graduated | in_progress"}]
  },
  "generation": {
    "target_words_min": 300,
    "target_words_max": 400,
    "allow_company_research": true,
    "letter_date": "YYYY-MM-DD"
  }
}
```

- `job.location` is `null` when no reliable location is known. Do not fill it in.
- `candidate.resume_v1` is plain text; `candidate.resume_v2` is the structured final resume object.
- `candidate.education_status` is computed by Python from the resume_v2 education dates and `generation.letter_date`: each entry is `graduated` (end date before the letter date) or `in_progress`.

### Education tense (mandatory)

- For a degree marked `graduated`, use past tense: "I recently completed …", "I graduated from … in April 2026", "I hold a Master of Professional Studies in Analytics …".
- Never write "I am completing / pursuing / finishing …", "currently a student", "will graduate" or "upcoming graduation" for a `graduated` degree. Python rejects such letters and retries.
- Use present/progressive tense only for a degree marked `in_progress`.

Python is responsible for locating and loading the correct job folder and `job.json`.

Do not search the local filesystem for another candidate profile or another version of the resume unless explicitly instructed.

---

## Source-of-Truth Hierarchy

Use the following hierarchy strictly.

### 1. `resume_v1` — factual source

`resume_v1` is the primary source for what the candidate has actually done.

It may contain useful experience or evidence that was later omitted from the final one-page resume because of space constraints.

Use it to determine:

- experience,
- projects,
- responsibilities,
- tools actually used,
- accomplishments,
- metrics,
- dates,
- education,
- other factual claims.

Do not invent facts that do not exist in the approved candidate information.

---

### 2. `resume_v2` — positioning source

`resume_v2` represents the final positioning chosen for this specific job.

Use it to understand:

- which experience should receive the most emphasis,
- which skills are most relevant,
- what terminology is appropriate for this role,
- how the candidate is being presented in the final resume.

The cover letter should complement the final resume rather than contradict or compete with it.

---

### 3. JD — target source

The job description defines what the employer is looking for.

Use the JD to identify the most important themes for the letter.

Do not attempt to cover every requirement.

Choose approximately **2–3 central themes** that have strong, truthful support in the candidate's experience.

Typical themes may include:

- data analysis,
- SQL / Python,
- data pipelines,
- BI / reporting,
- stakeholder communication,
- data quality,
- business problem solving,
- cloud analytics,
- operational analysis,
- cross-functional collaboration.

The selected themes must be driven by the actual JD.

---

## Core Writing Principle

A cover letter is **not a prose version of the resume**.

Its role is to explain:

1. why this specific role is relevant to the candidate;
2. which 1–2 experiences best demonstrate the fit;
3. how those experiences connect to the employer's needs;
4. why the role or company is worth pursuing.

The basic unit of writing should be:

**claim + evidence + relevance**

Avoid simple skill listing.

Weak:

> I am proficient in Python, SQL, Power BI and cloud tools.

Better:

> While working with large-scale analytics data, I used Python and SQL to turn complex datasets into usable outputs for downstream decision-making, an experience that aligns closely with the role's emphasis on...

---

## Recommended Structure

Always write **exactly 4 paragraphs**, one for each semantic key of the output object:
`opening`, `technical_fit`, `company_fit`, `closing`.

Do not merge two roles into one paragraph and do not add a fifth paragraph.

### `opening` — Opening / Positioning

Quickly establish:

- the role being applied for;
- the candidate's relevant professional identity;
- why this opportunity is particularly relevant.

Do not waste space introducing the candidate's name.

Avoid generic openings such as:

> I am writing to express my interest in...

unless the rest of the sentence immediately becomes specific and useful.

---

### `technical_fit` — Primary Evidence

Use the strongest experience that maps to the JD's highest-priority theme.

Explain:

- the context,
- what the candidate did,
- the relevant tools / methods,
- why this experience matters for the target role.

Do not simply repeat resume bullets word-for-word.

---

### `company_fit` — Secondary Evidence and Company Fit

Use a second complementary theme.

This may demonstrate:

- another technical capability,
- business impact,
- stakeholder communication,
- ambiguity handling,
- project delivery,
- cross-functional work,
- data storytelling,
- adaptability.

Choose evidence that adds something new rather than repeating `technical_fit`.

This is the most natural place for one company-specific idea, if official research produced one (see Company Research). Explain why the role or company fits the candidate's experience or direction; do not turn it into generic corporate praise.

---

### `closing` — Closing

Briefly summarize the fit, express interest in a further conversation, and close professionally.

Do not repeat everything said earlier.

The closing should be confident, concise, and professional.

Do not include exaggerated praise.

Do not write that the candidate is a "perfect fit".

Do not make promises that cannot be supported.

---

## What Not to Do

Do not:

- invent experience;
- invent metrics;
- invent technologies;
- invent certifications;
- change job titles;
- change employment dates;
- claim direct experience where only adjacent experience exists;
- turn nice-to-have JD items into candidate claims;
- copy large sections of the JD;
- repeat resume bullets verbatim;
- create a long list of tools;
- describe every JD requirement;
- write empty enthusiasm;
- volunteer weaknesses or missing qualifications;
- say "although I lack..." unless the application explicitly requires an explanation;
- exaggerate company admiration;
- describe company culture as fact without reliable evidence.

If the candidate does not meet a requirement, simply focus on the strongest truthful evidence that is relevant.

Let the employer evaluate remaining gaps.

---

## Company Research

Company research is optional.

If `allow_company_research` is false, do not browse.

If research is allowed, use it only to make the motivation more specific.

### Official sources only

Any concrete company fact written into the letter must come from an **official company source**:

1. the company's official website (including About / Careers / Values pages);
2. the company's official business, product or service pages;
3. the company's official newsroom / press releases;
4. the company's official annual report or investor materials.

The job description itself also counts as an official employer statement.

Third-party sources (news articles, job boards, review sites, directories, Wikipedia, LinkedIn, aggregators, AI summaries) may be used only to locate the official site. They must never be the source of a company fact in the letter.

If a fact cannot be confirmed on an official source, do not use it. If no official source can be found, or it contains nothing specific and useful, write the letter from the JD and candidate evidence alone. This is a normal outcome, not a failure.

Do not conduct broad or open-ended research.

Usually **1–3 targeted searches** are enough.

---

## How Company Research May Be Used

Official-source research may support statements about:

- the company's business area,
- a specific initiative,
- a relevant product or service,
- a stated company priority,
- a concrete aspect of the organization's work,
- a company value when it is genuinely relevant to the role.

Use at most **one strong company-specific idea** unless there is a clear reason for more.

The cover letter should still be primarily driven by:

**JD + candidate evidence**

A useful default balance is:

- 70–90% role and candidate fit;
- 10–30% company-specific motivation.

---

## Research Must Never Become Candidate Evidence

Web research may describe the company.

It may **not** be used to create candidate claims.

Candidate facts must come from the approved candidate sources.

Bad logic:

> The company uses Snowflake, therefore the candidate has Snowflake experience.

Never do this.

---

## Avoid Generic Culture Language

Do not force company culture into the letter merely because the website contains generic values such as:

- integrity,
- innovation,
- collaboration,
- excellence.

Avoid sentences such as:

> I have long admired your commitment to innovation and excellence.

unless there is specific, meaningful evidence that makes the statement relevant.

If research does not produce a useful insight, simply omit it.

---

## Style

Write in natural professional English.

The tone should be:

- concise,
- confident,
- specific,
- evidence-based,
- natural,
- not overly formal,
- not robotic,
- not promotional.

Prefer concrete language over corporate clichés.

Avoid excessive use of:

- "passionate",
- "thrilled",
- "excited",
- "perfect fit",
- "dynamic team",
- "prestigious organization",
- "fast-paced environment",
- "leverage my skills",
- "synergy".

Use Canadian English when spelling differences matter.

Do not use em dashes.

Use normal paragraph prose.

Do not output Markdown formatting inside paragraph text.

---

## Length Target

Generation target:

- **exactly 4 paragraphs** (`opening`, `technical_fit`, `company_fit`, `closing`)
- approximately **300–400 words** in total across the four paragraphs

The target is intentionally approximate.

The Python validator may allow a slightly wider tolerance.

Do not add filler merely to reach the minimum word count.

Do not compress useful evidence unnaturally merely to hit the exact maximum.

Quality and specificity matter more than landing on an exact number.

---

## Output Format

Return **one JSON object only**.

Do not return Markdown.

Do not return explanations before or after the JSON.

Do not return HTML.

Do not include the candidate's name, date, recipient / company address, salutation, signature block, or formatting metadata. Python adds those deterministically.

Required structure:

```json
{
  "paragraphs": {
    "opening": "Opening / positioning paragraph...",
    "technical_fit": "Primary evidence paragraph...",
    "company_fit": "Secondary evidence and company fit paragraph...",
    "closing": "Closing paragraph..."
  }
}
```

`paragraphs` is an **object with exactly these four keys**, not an array.

---

## Output Rules

- The root object contains only `paragraphs`.
- `paragraphs` contains exactly `opening`, `technical_fit`, `company_fit`, `closing`; no missing and no extra keys.
- Each value must:
  - be a non-empty string;
  - be one complete prose paragraph (no line breaks splitting it into several paragraphs);
  - contain no Markdown (bullets, headings, bold, code fences);
  - contain no HTML;
  - contain no placeholder tokens such as `{{company}}` or `[Company Name]`;
  - contain no notes to the user;
  - contain no citations or URLs.

If company research was used, incorporate only the resulting insight into the prose.

Do not append a research report.

---

## Lightweight Validation Expectations

Python is expected to validate at minimum:

- valid JSON;
- object contains only `paragraphs`;
- `paragraphs` is an object with exactly `opening`, `technical_fit`, `company_fit`, `closing`;
- no empty paragraph;
- total word count is approximately within the permitted range;
- no obvious placeholders;
- no Markdown / HTML structure.

A reasonable implementation may use:

- generation target: 300–400 words;
- validator tolerance: approximately 270–450 words.

There is no resume-style PDF overflow trim loop for cover letters.

If validation fails, regenerate or correct the structured content before HTML rendering.

---

## HTML / PDF Boundary

The worker must never generate the final HTML layout.

Python is responsible for:

1. receiving the JSON object;
2. validating it;
3. inserting the four semantic paragraphs into their fixed slots in the cover-letter HTML template;
4. adding deterministic metadata: date, recipient lines, salutation, and signature;
5. rendering the final PDF.

The fixed HTML template controls all visual formatting.

Do not attempt to solve layout issues by changing the content schema.

---

## Final Check Before Returning

Before returning the JSON, verify:

1. Every candidate claim is supported by `resume_v1`, `resume_v2`, or other explicitly approved candidate facts.
2. The letter focuses on approximately 2–3 important JD themes.
3. The strongest evidence appears early.
4. The letter complements rather than repeats the resume.
5. No missing qualification is unnecessarily highlighted.
6. Any concrete company fact comes from an official company source or the JD; anything else has been removed.
7. No company research has been converted into a candidate claim.
8. The prose sounds specific to this position rather than reusable for any company.
9. The output is valid JSON only.
10. `paragraphs` is an object with exactly the four semantic keys, and the total word count is approximately 300–400.
