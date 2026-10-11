# Candidate Capability Profile — JD Screening Version

*Fictional demo candidate (Morgan Ellis). Same structure as a real profile; every fact is invented.*

## Purpose

This file is a compact capability profile for **job-description screening**.

It is **not** a resume, bullet bank, or ATS keyword dump.
Its purpose is to let an AI judge whether a job's **core responsibilities** are supported by real prior experience without loading the full experience repository.

### Evidence Scale

- **STRONG** — repeated or substantial hands-on evidence; safe to treat as a direct capability match.
- **WORKING** — hands-on evidence exists, but scope is narrower, academic, or less production-like; usually an adjacent or credible match.
- **CONCEPTUAL / LIMITED** — exposure, framework design, or transferable understanding exists, but there is not enough executed evidence to treat it as a direct match.
- **NO EVIDENCE** — do not infer this capability from related tools or keywords.

When screening a JD, prioritize **functional responsibility match** over exact tool match.

---

# 0. Eligibility Facts

These are confirmed facts about the candidate's eligibility. They are the **only** basis for STUDENT_ELIGIBILITY, LANGUAGE and work-authorization / citizenship judgments. Do not infer eligibility from the capability sections below.

### Student / Enrollment Status

- **Graduated in April 2026.** Not currently enrolled in any program and not returning to school.
- **Not eligible** for any role that requires current enrollment, returning to school, or registration in a co-op / internship / work-term program.
- **Not eligible** for roles that require graduating within a window that extends into the future (e.g. "Must be graduated between May 2025 and May 2027"). Treat them as STUDENT_ELIGIBILITY rejects **even though April 2026 falls inside the window**.
- **Eligible** for roles open to recent graduates ("New Grad", "recent graduate", entry-level), and for internships that explicitly accept recent graduates without requiring enrollment.

### Work Authorization

- **Authorized to work in Canada for any employer**; does not require visa sponsorship.
- Is **not** a Canadian citizen. A role that strictly requires Canadian **citizenship** (not merely "legally eligible to work in Canada") is not met.

### Language

- **English only for work purposes. Does not work in French.**
- Reject **every** role that involves French in any way: French job title; the JD mentions French in any form (required, bilingual, preferred, asset); the posting content is written in French.
- **Exception — French boilerplate only:** if the **only** French in the JD is generic company boilerplate (company introduction, diversity / accommodation statements, legal notices), and the title is not French and French is not a requirement / asset, the role is **kept**.

---

# 1. Data Analysis & Data Preparation

## STRONG

### Data Cleaning, Transformation & Standardization
- Consolidated 3 years of multi-sheet carrier invoices into one analysis-ready dataset (Northwind Freight).
- Standardized carrier names, units and rate codes with rule-based mapping and regex; cut duplicate carrier records by 31%.
- Parsed free-text visit notes into 24 standardized labels (Fabrikam Health).

Typical responsibility matches: clean and prepare data, standardize datasets, resolve data-quality issues, create derived features.

### Exploratory & Descriptive Analysis
Aggregation, segmentation, trend and KPI analysis across healthcare, logistics, retail and rental-market data.

---

# 2. SQL & Cloud Data Processing

## STRONG
- BigQuery SQL and PySpark on GCP Dataproc over 120M records / 95 GB.
- SQL-based feature extraction (40 features from range-coded fields).

## WORKING
- AWS S3 / Glue / Athena / SageMaker in a course pipeline.
- PostgreSQL for course work.

Do **not** infer: database administration, enterprise warehouse architecture, Snowflake or Databricks platform ownership.

---

# 3. Data Engineering & Pipelines

## STRONG
- Automated Python ETL pipeline (Northwind Freight).
- Two-stage BigQuery + Python text-to-label pipeline (Fabrikam Health).
- Python REST API enrichment workflow (delivery-date completeness 72% → 96%).

## WORKING
- Workflow orchestration concepts through Glue and multi-stage pipelines. Airflow / dbt / Kafka are **tool gaps**, not direct experience.

---

# 4. Business Intelligence & Dashboards

## STRONG
- Power BI dashboard used by operations to prioritize contract renegotiations (14 regions).
- Looker KPI dashboard for clinical operations managers.
- Tableau dashboard over 85K rental listings.

## WORKING
- Executive-style reporting through internship dashboards; no long corporate reporting tenure.

---

# 5. Machine Learning & Statistics

## STRONG
- Supervised classification: XGBoost, Random Forest, Logistic Regression (readmission, churn).
- Model evaluation: precision / recall, ROC-AUC, cross-validation, baseline comparison.
- Regression and hypothesis testing (course work).

## WORKING
- Time-series forecasting (SARIMA, gradient boosting; MAPE 18.4% → 11.9%).
- Class-imbalance handling, hyperparameter tuning.

Do **not** infer: production MLOps, model serving, deep learning, LLM / RAG engineering.

---

# 6. Business Analysis & Stakeholders

## STRONG
- Biweekly requirement sessions with operations managers; KPI definitions and self-serve views.

## CONCEPTUAL / LIMITED
- Supply-chain optimization framework (designed, **not** implemented).

Do not infer formal people management.

---

# 7. Primary Tools — Evidence-Based

**Strong:** Python, SQL, BigQuery, PySpark, GCP Dataproc, pandas, scikit-learn, XGBoost, Power BI, Looker, Tableau, Excel, REST APIs, Regex

**Working:** R, AWS S3 / Glue / Athena / SageMaker, PostgreSQL, statsmodels

**Do not infer as direct experience:** Airflow, dbt, Kafka, Snowflake, Databricks, Kubernetes, Docker, Terraform, production CI/CD, enterprise data governance, backend / microservices engineering

---

# 8. Responsibility-Matching Rules for JD Screening

- **Direct match**: the JD responsibility is substantially the same work already demonstrated (ETL, data cleaning, SQL / Python analysis, dashboards, API integration, large-scale processing, feature engineering, predictive models).
- **Adjacent match**: same underlying function, different tool or platform (Airflow ← multi-stage pipelines; Snowflake ← BigQuery; Azure ← GCP / AWS).
- **Learnable tool gap**: a missing tool does not by itself cause rejection when the candidate already performs the function, the role is junior, and the tool is not a hard requirement.
- **Structural gap**: the core work is outside demonstrated capability (backend engineering, platform / DevOps, security engineering, DBA, mandatory specialized domain experience).

Do not require exact tool equality when the JD asks for a function already demonstrated with another comparable tool.
Do not upgrade conceptual, academic, or framework-only exposure into production ownership.
