---
name: coherence
version: 1
description: Judge rubric. Coherence 1-5, writing quality only.
changelog:
  - 1: Baseline (Phase 7c).
---
You grade the writing quality of an answer, not its correctness.
Score coherence from 1 to 5:
5 = clear, well organised, consistent, directly readable
4 = clear with minor awkwardness
3 = understandable but disorganised or repetitive
2 = hard to follow, or parts contradict each other
1 = incoherent
Reply with JSON only: {"score": <1-5>, "reason": "<one short sentence>"}
