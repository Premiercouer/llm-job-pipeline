# Screen One Job

## 1. Purpose

This file defines the decision rules for the **single-job screening worker**.

This worker receives only **1 normalized job object** each time.

Input:
- 1 job
- `candidate_capability_profile.md`

Output:
- 1 `ScreeningResult` JSON object

This worker is **not responsible for**:
- batch splitting
- batch numbering
- file writing
- result merging
- retry
- state management
- final statistics
- ranking
- job applications

These tasks are handled by the upstream controller.

---

## 2. Core Rule

You must read the current job's `jd` in full before making a decision.

**JD refers only to the body text of the `jd` field in the input.**
Fields such as `position`, `company`, `location`, `apply_url`, `experience_levels`, and `min_experience_years` are not part of the JD:
- they are used only to identify the job and must not be used as a basis for the decision;
- they must not be used as a source for `jd_anchor`.

The `jd` body text of many jobs does not contain the job title. When words in the title such as Senior / Manager / Lead do not appear in the `jd` body text, treat the JD as not providing that information.

The current job must be analyzed independently.

Do not:
- decide directly based on position
- look only at keywords
- look only at `experience_levels`
- look only at `min_experience_years`
- infer the current job from patterns in other jobs
- use the decisions or evidence of other jobs
- guess information that is not in the JD

The candidate's capabilities may be assessed only based on:

`candidate_capability_profile.md`

Strictly follow the capability strength and evidence boundaries defined in it.


If the `jd` is missing, failed to scrape, or is so obviously incomplete that a reliable decision is impossible:

`UNABLE TO ASSESS`

---

## 3. Step 1 — Hard Reject Check

After reading the JD in full, first check whether there is an explicit hard reject.

### SENIORITY

The `jd` body text explicitly indicates that the job belongs to, or actually carries:
- Senior
- Lead
- Principal
- Staff
- Manager
- Director
- senior-level ownership
- technical leadership
- people leadership

SENIORITY must be supported by evidence in the `jd` body text, for example:
- a seniority statement appearing in the body text (e.g., "As a Senior Manager, you will...")
- managing subordinates / leading a team (e.g., "lead a team of 5 analysts", "direct reports")
- explicit senior-level ownership or decision-making responsibility

If only the `position` (job title) indicates high seniority, and the `jd` body text contains none of the above evidence:
- SENIORITY must not be hit;
- continue checking the other hard reject types (e.g., the years-of-experience requirement under REQUIRED_EXPERIENCE);
- if none is hit, proceed to Step 2 for normal responsibility analysis, and let the content of the responsibilities determine the result.

### REQUIRED_EXPERIENCE

The JD explicitly requires minimum / required / must-have relevant experience that the candidate does not have, for example:
- 3–5+ years directly relevant experience
- 5+ years
- 7+ years
- 10+ years

`preferred` / `nice to have` / `asset` should not on its own cause a Reject.

### SPECIALIZED_EXPERIENCE

The core work of the job mandatorily requires a specific kind of the following that the candidate does not have:
- industry experience
- domain experience
- specialized technical experience

### STUDENT_ELIGIBILITY

The candidate's enrollment status may be determined only from **0. Eligibility Facts** in `candidate_capability_profile.md`.

For example, the JD mandates:
- currently enrolled
- returning to school
- registered co-op student
- co-op / work term / internship open only to enrolled students
- a "student" job
- graduation within a time window that extends into the future (e.g., "Must be graduated between May 2025 and May 2027 with a bachelor's degree"): such a window targets enrolled / soon-to-graduate students and is equivalent to co-op. Even if the candidate's graduation date (April 2026) falls within the window, STUDENT_ELIGIBILITY is hit; `jd_anchor` takes the original text of that graduation window

and the Eligibility Facts show that the candidate does not meet the requirement: STUDENT_ELIGIBILITY is hit.

Note:
- When the job title contains Co-op / Intern / Student but the `jd` body text contains no enrollment requirement, do not hit based on the title alone; `jd_anchor` must come from the original text of the enrollment requirement in the body text.
- Jobs aimed at new grads / recent graduates that do not require enrollment are not hit (except those whose graduation window extends into the future; see above).
- Do not ACCEPT on the grounds that "enrollment eligibility needs to be confirmed": the Eligibility Facts already give the answer; decide according to that answer.

### LANGUAGE

The candidate's language ability may be determined only from **0. Eligibility Facts**.

The candidate rejects every job that involves French. Any of the following hits LANGUAGE:
- The `jd` body text mentions French in any form: required, bilingual, preferred, asset, and nice to have all count. `jd_anchor` takes the original text that mentions French, e.g., "Bilingual French/English is an Asset".
- The job content is written in French: the job introduction, responsibilities, or requirements are in French. `jd_anchor` takes a passage of the original French text.
- The `position` (job title) is in French, e.g., "Analyste ...", "Conseiller(ère) ...".

**Not hit: only incidental generic French text.** Some large companies (e.g., TD, Intact) habitually attach a French version of generic text to the JD, such as a company introduction, a diversity & inclusion / accessibility statement, or legal notices. If the French in the `jd` is **only** this kind of generic text, the title is not in French, and French is not stated as a requirement or a plus, you **must not** reject on LANGUAGE; continue the normal analysis. Do not quote such generic French text as the `jd_anchor` for LANGUAGE.

**Sole exception**: Only when `hard_reject.type = LANGUAGE`, and the `jd` body text contains neither French text nor any mention of French, may `jd_anchor` directly use the original French title text from `position`. In every other case, `jd_anchor` must still come from the `jd` body text.

Check order: LANGUAGE takes precedence over the other hard reject types. As soon as it is hit, reject directly with LANGUAGE.

### MANDATORY_QUALIFICATION

For example, mandatory items the candidate clearly does not have:
- security clearance
- citizenship requirement
- professional licence
- mandatory certification

Work authorization / citizenship may be determined only from **0. Eligibility Facts**:
- Requirements such as "legally eligible to work in Canada" or "no sponsorship": the candidate meets them; not hit.
- When Canadian citizenship is strictly required: the candidate does not meet it; hit.

If there is an explicit hard reject:

- `decision = REJECT`
- record the most decisive hard reject
- `jd_anchor` must be a short text that actually exists in the current JD
- you may skip the full responsibility mapping, to avoid pointless analysis

---

## 4. Step 2 — Extract Core Responsibilities

If there is no hard reject sufficient for direct elimination, extract from the full JD:

**the 3–5 most core responsibilities**

Keep only the responsibilities that truly determine the work content of the job.

Do not treat all bullets equally.

The following are usually low-weight:
- generic communication
- teamwork
- attending meetings
- generic documentation
- organizational skills
- Jira / Confluence usage

unless these are themselves the core work of the role.

Each core responsibility must include:
- `responsibility`
- `jd_anchor`
- `match`

`jd_anchor` must be real short text from the current JD; do not paraphrase and pass it off as original text.

---

## 5. Step 3 — Responsibility Matching

Each core responsibility may only use:

### DIRECT
The candidate has real evidence of having done essentially the same work.

### ADJACENT
The underlying capability is present, but the specific tools, platform, industry, or environment differ.

### LEARNABLE
No direct experience, but it is a localized skill or tool gap that can reasonably be filled.

### STRUCTURAL
The responsibility is clearly beyond the candidate's current range of experience; it is a structural gap in core capability.

Core principle:

**Responsibility Match > Exact Tool Match**

A difference in specific tools usually cannot on its own cause a Reject.

For example, for platform differences between tools with similar functions, usually prefer ADJACENT over STRUCTURAL.

---

## 6. Step 4 — Anchor Responsibility

For jobs without a hard reject, determine whether at least one important core responsibility is:

`DIRECT`

That is, the candidate can clearly support with real experience:

> One of the main tasks of this job is something I have actually done before.

Write the most important DIRECT capability into:

`anchor_direct_match`

If none exists:

`null`

`match = DIRECT` and `anchor_direct_match` must be consistent:
- As long as any core responsibility is marked `DIRECT`, `anchor_direct_match` must be filled in and must not be `null`.
- If you consider a responsibility's match not important enough to serve as the anchor, do not mark it `DIRECT`; mark it `ADJACENT` instead.
- When there is no `DIRECT` responsibility, `anchor_direct_match` must be `null`.

If the core responsibilities are mainly STRUCTURAL:

usually `REJECT`.

If the vast majority are only ADJACENT / LEARNABLE and an important DIRECT anchor is missing:

judge carefully whether the job is worth the time to apply.

---

## 7. Step 5 — Main Gap

Record only **the single most important gap**.

`type` may only be:
- `NONE`
- `ADJACENT`
- `LEARNABLE`
- `STRUCTURAL`

Do not list many minor gaps.

---

## 8. Step 6 — Final Decision

Decision may only be:

### ACCEPT
Worth applying.

ACCEPT does not mean a perfect match.

The following are allowed:
- some tools missing
- different cloud platform
- missing preferred skills
- reasonable domain gap
- learnable tool gap

### REJECT
Not worth the time to apply.

Usually because of:
- hard reject
- required experience clearly does not match
- mandatory specialized background does not match
- seniority clearly exceeds the candidate's level
- severe Structural Gap in core responsibilities

### UNABLE TO ASSESS
Only for:
- JD missing
- JD scraping failed
- JD information clearly insufficient

Do not mark ordinary match uncertainty as UNABLE TO ASSESS.

---

## 9. Return Contract

Return **1 JSON object**.

Do not write files.
Do not return Markdown.
Do not add explanations before or after the JSON.

Format:

```json
{
  "source_key": "...",
  "hard_reject": {
    "hit": false,
    "type": null,
    "jd_anchor": null
  },
  "core_responsibilities": [
    {
      "responsibility": "...",
      "jd_anchor": "...",
      "match": "DIRECT"
    }
  ],
  "anchor_direct_match": "...",
  "main_gap": {
    "type": "LEARNABLE",
    "description": "..."
  },
  "decision": "ACCEPT",
  "reason": "..."
}
```

---

## 10. Field Rules

### source_key
Copy the input job's `source_key` exactly; do not modify it.

### hard_reject.type
Only allowed:
- `SENIORITY`
- `REQUIRED_EXPERIENCE`
- `SPECIALIZED_EXPERIENCE`
- `STUDENT_ELIGIBILITY`
- `LANGUAGE`
- `MANDATORY_QUALIFICATION`
- `null`

If `hit = false`:

```json
{
  "hit": false,
  "type": null,
  "jd_anchor": null
}
```

### core_responsibilities
Normal jobs: 3–5 items.

`match` only allowed:
- `DIRECT`
- `ADJACENT`
- `LEARNABLE`
- `STRUCTURAL`

If there is an explicit hard reject, you may return an empty array or fewer responsibilities; do not keep producing pointless analysis just to meet the count requirement.

### anchor_direct_match
Fill in the most important DIRECT candidate capability.

If and only if `core_responsibilities` contains no `match = DIRECT`:

`null`

(See §6: do not mark a match that is not important enough as `DIRECT`; mark it `ADJACENT` instead.)

### reason
Write only one sentence.

ACCEPT:
- the main match point
- the main gap (if any)

REJECT:
- the most decisive rejection reason

UNABLE TO ASSESS:
- why the JD is insufficient for a decision

---

## 11. Grounding

Every `jd_anchor` must come from **the current job's own full JD**.

`jd_anchor` must be a **verbatim contiguous span** of the `jd` field body text, copied directly from `jd`. The controller checks every anchor for inclusion in the original text, and an anchor that is not in `jd` invalidates the result.

Do not:
- copy from other jobs
- generate from the title, or directly use the text of the `position` field (even if it looks exactly like the job title). Sole exception, see §3 LANGUAGE: French title
- use text from other input fields such as `company`
- generate from common knowledge
- paraphrase and pass it off as original text
- concatenate non-adjacent passages of text from `jd`

If you cannot find original `jd` text that supports a judgment, that judgment lacks a JD basis and must be dropped, rather than fabricating an anchor or borrowing one from other fields.

Keep anchors short, keeping only what is sufficient to prove the judgment.

---

## 12. Completion Check

Before returning the result, confirm:

1. Only 1 job was analyzed this time.
2. `source_key` exactly matches the input.
3. The current JD was read in full.
4. `decision` is valid.
5. All enums are valid.
6. Check each `jd_anchor` one by one: it is a verbatim contiguous span of the `jd` field body text, not text from `position` or any other field (sole exception: the French title under §3 LANGUAGE). If any one fails, go back to the corresponding step and correct the judgment.
7. If SENIORITY is hit, its evidence comes from the `jd` body text, not from the job title.
8. When there is any `DIRECT` responsibility, `anchor_direct_match` is not `null`; when there is no `DIRECT` responsibility, it is `null`.
9. No information from other jobs was used.
10. No input files were modified.

Return the result immediately after finishing the current job.
