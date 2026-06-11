# Clarify Financial Period — Copilot Studio build sheet

Deterministic period clarification for the FinSight US agent. Pairs with the
**PERIOD ROUTING FILTER** instruction (in `pipeline/agents/specs.py` /
`COPILOT_STUDIO_SETUP-v2.md`) so a financial KPI question with no stated period
asks **Monthly / Quarterly (YTD) / Full year (outlook)** and then filters the
search to the matching `period_scope`.

Why a topic (not instructions only): per Microsoft Learn, instruction-based
follow-up questions can be suppressed when *Allow ungrounded responses* is off.
A Topic + Question node is deterministic and unaffected by that setting.

> Build everything below on the **PARENT** agent (it owns Entities + Topics).
> The **Financials sub-agent** only needs its updated Instructions re-pasted.
> Item values (`month` / `ytd` / `full_year`) are lowercase and MUST match the
> Azure AI Search `period_scope` field values exactly.

---

## (a) Entity: `PeriodScope`

Settings → Entities → **+ New entity → Closed list** → name `PeriodScope`.
Add three items; paste the synonyms (one per line) into each item's synonym box.

### Item value: `month`
```
monthly
month
MTD
month to date
single month
this month
for the month
in the month
January
February
March
April
May
June
July
August
September
October
November
December
```

### Item value: `ytd`
```
quarter
quarterly
quarter to date
QTD
Q1
Q2
Q3
Q4
first quarter
year to date
year-to-date
YTD
March YTD
so far this year
cumulative
```

### Item value: `full_year`
```
full year
full-year
FY
fiscal year
annual
for the year
outlook
Latest Outlook
Mar LO
March LO
LO
forecast
full year outlook
```

---

## (b) Topic: `Clarify Financial Period`

Topics → **+ Add a topic → From blank** → name it exactly `Clarify Financial Period`.

### Trigger (type: "The agent chooses") — description
```
Use this topic when the user asks for a US financial KPI or dollar figure - such as net sales, cost, gross margin, OPEX, operating income, gross-to-net (GTN), PVM, growth, or "vs target / vs PY / vs LO" - but does NOT specify the time period. Ask whether they want a single month, the quarter / year-to-date, or the full-year outlook before answering. Do not use this topic for external messaging, guidance, or product-strategy / volume questions; only for reported financial close figures. Skip clarifying if the user clearly wants the latest or overall view.
```

### Node 1 — Ask a question
- **Message:**
```
Which period would you like this for — monthly, quarterly (year-to-date), or the full-year outlook?
```
- **Identify:** Multiple choice options, with options (exact text):
```
Monthly
Quarterly (YTD)
Full year (outlook)
```
- Also attach the **`PeriodScope` entity** under Identify (auto-skip when the user already said e.g. "Q1").
- **Save user response as:** `PeriodChoice`

### Node 2 — Set variable value
- Create a **Global** string variable `SelectedPeriodScope`.
- **To value → Formula** (Power Fx):
```powerfx
Switch(
    Topic.PeriodChoice,
    "Monthly", "month",
    "Quarterly (YTD)", "ytd",
    "Full year (outlook)", "full_year",
    "ytd"
)
```

Safer variant (prefers the multiple-choice answer, falls back to the entity
value when the user already stated the period, then defaults to ytd):
```powerfx
Coalesce(
    Switch(
        Topic.PeriodChoice,
        "Monthly", "month",
        "Quarterly (YTD)", "ytd",
        "Full year (outlook)", "full_year",
        Blank()
    ),
    Topic.PeriodScope,
    "ytd"
)
```

### Node 3 — (optional confirmation)
```
Got it — showing the {Global.SelectedPeriodScope} figures.
```

---

## Wiring to the answer
- The topic sets `Global.SelectedPeriodScope` = `month` / `ytd` / `full_year`.
- The Financials sub-agent **PERIOD ROUTING FILTER** instruction applies
  `period_scope eq '<value>'`.
- If the Azure AI Search tool is called with an explicit filter parameter, set:
```
period_scope eq '{Global.SelectedPeriodScope}'
```

---

## Verify after Publish (new chat)

| Test input | Expected |
| --- | --- |
| "What were Pluvicto net sales?" | Asks: Monthly / Quarterly (YTD) / Full year |
| Choose **Quarterly (YTD)** | Answers from the YTD grid (e.g. Pluvicto GTN = 11) |
| "Pluvicto **Q1** gross-to-net vs target" | Skips question (entity auto-fills), answers the YTD grid |
| "Pluvicto **March** net sales" | Skips question, answers the monthly grid |
| "How are we doing overall?" | Does NOT ask; uses latest period and states it |

Troubleshooting:
- Asks even when period given → add the missing word to `PeriodScope` synonyms.
- Wrong grid after a choice → confirm the `Switch` outputs lowercase
  `month` / `ytd` / `full_year` exactly (must match the index field).
- Use the test panel **activity map** to watch the topic fire and inspect the
  `SelectedPeriodScope` variable.

References (Microsoft Learn):
- Configure high-quality instructions for generative orchestration (follow-up questions; ungrounded-responses caveat)
- Use entities and slot filling in agents
- Implement slot-filling best practices
- Ask a question (Question node)
