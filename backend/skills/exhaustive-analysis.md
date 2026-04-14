---
name: exhaustive-analysis
description: GStack Boil the Lake — exhaustive enumeration before selection
metadata:
  evermind:
    applies_to: [planner, analyst]
---

# Exhaustive Analysis Protocol (Boil the Lake)

## Core Principle
Before choosing ANY approach, enumerate ALL viable options first.

## Option Enumeration
For every architectural, framework, or strategic decision:
1. List at least 3 alternatives.
2. For each alternative, state:
   - Pros (specific, not generic)
   - Cons (specific, not generic)
   - Confidence score 1-10 (how sure are you this will work?)
   - Evidence (URL, code reference, or reasoning chain)
3. Select the highest-confidence option.
4. If ALL options score below 5: stop and escalate. Output a <wtf_escalation> block explaining what information is missing to make a confident choice.

## Research Depth Requirements
- For architecture decisions: survey at minimum 3 reference implementations.
- For technology choices: check compatibility, performance data, community support.
- For design patterns: verify the pattern fits the actual constraint, not just the category.

## Output Format
Every recommendation must include:
- The chosen option (named explicitly)
- Why it was chosen over alternatives (1-2 sentences)
- Confidence level (1-10)
- Risk factors (what could go wrong)
