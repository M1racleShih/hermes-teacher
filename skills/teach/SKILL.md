---
name: teach
description: Assess prerequisites, agree on a lesson plan, and teach step by step in a Teacher classroom.
version: 0.3.0
---

# Classroom teaching

Use this workflow when a learner submits a goal, starts a lesson, or resumes teaching. The learner should be able to derive new ideas from prior knowledge and demonstrate understanding independently.

## Workflow

### 1. Confirm the goal and assess prerequisites

During setup, answer questions about model choice and reasoning effort. Wait for a learning goal or an explicit request to start. If the learner's intent is clear and the goal has yet to be recorded, use `teacher_ask` with `purpose=goal` and `mode=text` to collect the goal in their own words. Use `teacher_ask` for preferences. Multiple-choice preferences can include a custom answer alongside listed choices.

Use `strands` in the first assessment quiz to list the goal's prerequisite strands. Set `strand` on each `teacher_quiz` to the relevant strand. Raise the difficulty after successive independent successes, and lower it when there is no evidence of independent success. After a wrong or unknown answer, ask a related question to distinguish a calculation slip, an isolated gap, and a broader misconception. Set `follows` to that strand's `gap.id` in `probe_progress`. Before planning, collect evidence of independent success, difficulty, and a follow-up in each strand. Set the scope from the learning goal.

### 2. Research and plan

Use `teacher_role` to start the researcher. End the turn while research runs if appropriate. The completed findings return to the original lesson. Check the sources actually read, then use `teacher_plan` to submit ordered nodes, direct dependencies, the rationale for the scope, and the research task ID. Use `prerequisite` and `dependent` to name each edge's endpoints. Several independent prerequisites can lead to one node. The final node is the learning goal.

Explain the scope, sequence, and reasoning in the plan. Show the Mermaid dependency graph. Wait for approval, and submit a revised plan for approval after feedback.

### 3. Teach one node at a time

For each important node:

1. State the problem to solve.
2. Establish facts and definitions, or derive results from earlier nodes.
3. Explain how the new idea relates to existing knowledge.
4. Check understanding with a quiz and address gaps in the answer.

Give the learner a chance to work out the result. When a derivation is beyond the current scope or the learner asks for direct teaching, explain how the result is obtained. Verify uncertain facts and formulas with the research role's evidence.

### 4. Ask questions and give feedback

- Base distractors on specific misconceptions and keep choices similar in structure and length.
- Choices are shuffled by default. If an order is meaningful, such as ascending numeric intervals, set `shuffle=false` and specify the order in the objective.
- Put explanations in the feedback shown after submission.
- Record independent success, success after a hint, a wrong answer, an unknown answer, and cancellation separately.
- Continue after the learner submits a real answer and acknowledges the feedback.

### 5. Save progress and resume

The classroom records visible exchanges automatically. At a transition or pause, use `teacher_checkpoint` to capture coverage, evidence from actual answers, gaps, material paths, and next steps.

On resuming, read the lesson state and teacher handoff. Briefly check the prerequisites for the next step and continue the plan. Follow the classroom's confirmation flow to seal a record or open a new session.

Read lesson materials with `teacher_material`. For long files, page with `start_line` and `line_count` and pass back the returned `next` fields to continue.

### Language and formatting

Use the learner's requested or apparent language. When it is unclear, use the classroom's interface language, which defaults to English. Explain unfamiliar terms on first use. Use `$...$` and `$$...$$` for math and delegate structural or geometric diagrams to the matching role.

## Verification

Check assessment evidence, plan approval, real learner answers, understanding at each node, and continuity after resuming. See the [runtime skills index](../README.md).
