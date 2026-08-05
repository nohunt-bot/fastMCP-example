---
name: text-stats
description: Word, line and character counts plus top-term frequencies for a block of text or a file. Use for readability checks, keyword extraction, or verifying a draft fits a length budget.
version: 1.0.0
tags: [text, analysis, writing]
---

# Text statistics

## Usage

Pipe text in via stdin (no temp file needed):

```
run_skill_script("text-stats", "scripts/wordcount.py", ["--top", "10"], stdin="...")
```

Or point it at a file:

```
run_skill_script("text-stats", "scripts/wordcount.py", ["/path/to/draft.md"])
```

## Output

JSON with `chars`, `words`, `lines`, `sentences`, `avg_word_length`,
`avg_sentence_length`, and `top_terms` (stopwords removed).

## Length budgets

`avg_sentence_length` above ~25 words usually reads as dense prose. Under ~8 it
reads as choppy. Neither is a rule — check the target audience first.
