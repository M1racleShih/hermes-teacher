---
name: lesson-research
description: Research lesson topics, verify facts, and return traceable evidence.
version: 0.1.0
---

# Lesson research

Use this workflow to plan a lesson or verify a definition, formula, fact, technical behavior, or teaching resource.

## Inputs and results

Provide the lesson identity, learning goal, questions to verify, prior knowledge, teaching language, and scope. Return conclusions in the requested teaching language, the sources you actually read, key evidence, applicable conditions, gaps, and suggestions for teaching.

Start a `researcher` task through `teacher_role` and retain the returned `id`. Use `wait` or `status` to retrieve progress. Verify `result.summary` against the URLs and excerpts in `result.sources`. Give `teacher_plan` the task ID from this lesson.

## Workflow

1. Break the request into a few questions that affect teaching decisions.
2. Search from several angles and prioritize official documentation, original papers, specifications, and source code.
3. Open the sources and read the supporting passages. Check versions and dates.
4. Investigate conflicting or important claims with more sources or a reproducible example.
5. Distinguish documented facts, inferences, and open questions. Return the actual findings to the teacher.
6. Identify a concrete way to address any remaining gap.

## Tools and recovery

The researcher uses Hermes `web_search` and `web_extract`. Use the same task ID with `supplement` to ask a follow-up, `cancel` to stop work, or `resume` to recover a failed or interrupted task. The task retains its lesson and role model. If a page cannot be read, inspect `error`, `result.searches`, and `result.failure` and explain which claims could be verified.

## Verification

Check that conclusions trace to sources read, with accurate versions and conditions. Record failures and gaps.
