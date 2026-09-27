---
name: analyzer
description: Turn source content or a verified transcript into a structured, evidence-grounded analysis with key insights, risks, and action items.
---

# Content Analyzer Skill

> Any content → structured analysis report with actionable insights

## Trigger

When user sends content (URL, text, or transcript) with analysis intent:
- `/analyze [URL]`
- "Analyze this article"
- "What are the key takeaways?"
- Auto-triggered after video/podcast transcription (from video skill)

## Pipeline

### Step 1: Get Content

Choose tool based on input type:

| Input | Tool |
|-------|------|
| Tweet URL | `fetch_tweet` or Jina Reader |
| Web URL | `WebFetch` or Jina Reader |
| Local file | Read file directly |
| Transcript from video skill | Use directly |

### Step 2: Multi-Dimensional Analysis

Scan content across these dimensions. Only output dimensions with actual content — skip empty ones.

```markdown
## 📖 Summary

[1-3 sentence core thesis]

**Source**: [author/publisher] · [date]
**Type**: [tweet/article/video/podcast/report]

---

## 💡 Key Insights

### 🎯 Core Arguments
- **Thesis**: [Main argument or finding]
- **Evidence**: [Supporting data or reasoning]
- **Strength**: [How convincing? What's missing?]

### 🤖 Tools & Methods
- **What**: [Tools, frameworks, or techniques mentioned]
- **How**: [How they're used or applied]
- **Relevance**: [Could you use this?]

### ⚙️ Workflow Ideas
- **Optimization**: [Process improvements mentioned]
- **Automation**: [What could be automated]
- **Integration**: [How to fit into existing workflow]

### 📊 Data & Numbers
- **Key metrics**: [Important numbers mentioned]
- **Trends**: [Patterns in the data]
- **Gaps**: [What data is missing]

### ⚠️ Risks & Warnings
- **Pitfalls**: [Explicitly mentioned risks]
- **Blind spots**: [What the author might be missing]
- **Counter-arguments**: [Alternative perspectives]

### 🔗 Resources
- **Tools/APIs**: [Mentioned tools or data sources]
- **People**: [Worth following or referencing]
- **Further reading**: [Related content]

### 🧠 Mental Model Shifts
- **Before**: [Common assumption]
- **After**: [New understanding from this content]
- **Impact**: [How this changes decisions]

---

## ✅ Action Items

### Quick Wins (< 30 min)
- [ ] [Action 1] — Impact: ★★★★ | Effort: Easy
- [ ] [Action 2] — Impact: ★★★ | Effort: Easy

### Deeper Work (1-3 hours)
- [ ] [Action 3] — Impact: ★★★ | Effort: Medium
- [ ] [Action 4] — Impact: ★★ | Effort: Medium

### Exploration (needs validation)
- [ ] [Action 5] — Impact: ★★★ | Effort: Hard | Nature: Exploratory
```

### Step 3: Personalized Relevance (Customizable)

Map insights to YOUR context. Edit the dimensions below to match your own projects, interests, and systems.

```markdown
## 🔄 How This Applies to Me

### My Projects
- **[Project A]**: [How this insight connects]
- **[Project B]**: [What I could apply]

### My Knowledge Base
- **Update**: [Which notes/docs to update]
- **New entry**: [What to add to my knowledge system]

### My Decision Log
- **Changed my mind about**: [what and why]
- **Confirmed my belief that**: [what]
```

> **Customization**: Edit the dimensions in Step 2 and Step 3 to match your own
> domain. A trader might add "Market Impact" and "Risk Assessment". A developer
> might add "Architecture Patterns" and "Tech Debt". Make it yours.

## Output Modes

| Mode | Trigger | Output |
|------|---------|--------|
| **Full** (default) | `/analyze [URL]` | All dimensions |
| **Sparse** | `/analyze [URL] --sparse` | Only hit dimensions, skip empty |
| **Brief** | `/analyze [URL] --brief` | Action items only |

## Best Practices

1. **Scan all dimensions, but don't force-fill** — skip empty dimensions cleanly
2. **Actions must be specific** — not "learn about X" but "read X docs chapter Y"
3. **Distinguish fact from opinion** — mark the author's claims vs verified facts
4. **Source everything** — tag where each insight comes from in the original content
5. **ROI awareness** — not every action is worth doing, assess effort vs impact
