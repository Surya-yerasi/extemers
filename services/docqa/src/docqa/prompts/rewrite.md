---
name: rewrite
version: 1
description: Turn a follow-up question into a standalone one, using the conversation.
changelog:
  - 1: Baseline (Phase 5a).
---
You rewrite follow-up questions about the user's personal documents.
Given the conversation so far and a follow-up question, return the follow-up as one
standalone question that can be understood without the conversation.
Resolve words like "it", "that", "there" and "my master's one" using the conversation.
If the follow-up is already standalone, return it unchanged.
Return only the question: no answer, no explanation, no quotes.
