---
name: metadata
version: 1
description: Extract document type, title, institution, person and date as JSON. {text} is the document start.
changelog:
  - 1: Baseline (Phase 2).
---
Read the start of this document and return ONLY a JSON object with keys:
doc_type (one of: transcript, degree_certificate, certificate, letter, id_document, other),
title, institution, person, date. Use "" when unknown. No other text.

Document:
{text}
