---
name: lesson-organizer
description: Organize sealed lesson records into chapters, concept cards, and review materials while preserving the source.
version: 0.2.0
---

# Lesson organization

Use when the learner requests organization of a sealed lesson or relocation of a topic vault or management root.

## Commands

In Teacher chat, use `/seal` to seal the current record. The same session writes to a new record when teaching continues. Run `/organize` in Teacher chat or `/teacher organize` in a native Hermes session to organize the sealed record.

The organizer uses a separate Hermes session with an empty toolset. From the sealed snapshot, it proposes chapter boundaries, concept cards, review questions, and suggestions for resuming. The output includes chapters, a reading entry, supporting notes, archived source text, and a source ledger. Write new material in the learner's requested language or the lesson's language, with the classroom language as fallback. Preserve the sealed source text verbatim.

## Workflow

1. Find the current vault, seal receipts, and prior chapters. Copy the sources referenced by the receipts to snapshots and verify their digests.
2. Choose chapter boundaries by concept. Keep question, answer, explanation, and feedback together. Preserve code, math, tables, and image references.
3. Build each concept card from the lesson's definitions, conditions, examples, and common misconceptions. Include specific retrieval prompts in review notes. Distinguish demonstrations, learner answers, and topics that need reassessment. Assign each card a primary source chapter. The organizer also links to all chapters of the source so readers can locate evidence across chapters.
4. Before applying, verify that each source line is covered exactly once, the document structure is complete, links resolve, and target files agree with the ledger. The organizer proposes chapter ranges and supporting notes. The managed process writes and archives them.
5. Check the chapters, reading entry, archive digests, and active record. Report the sealed sources, output paths, and any remaining work.

Run the commands below from the installed `plugins/hermes-teacher/` directory or repository root. Set `--profile` to an initialized Teacher profile. Use `scan` to inspect seal receipts and `snapshot` to capture sources for a manual plan.

```sh
bin/hermes-teacher --profile /path/to/profile assets scan --vault /path/to/vault
bin/hermes-teacher --profile /path/to/profile assets snapshot --vault /path/to/vault --topic 'TOPIC'
```

The snapshot manifest records source IDs, line counts, and digests. To plan chapters manually, save a `bundle.json` with `schema_version: 2` in the returned run directory. Each `chapters` entry contains `slug`, `title`, and `slices`. Each slice has `id`, `start`, and `end`. Line numbers start at 1 and are inclusive. Optional `concepts`, `reviews`, and `materials` entries contain `slug`, `title`, and `body`, with the body linked to the relevant chapters. Use `next_session` for reading references. Use the classroom's verified teacher handoff to resume teaching.

```sh
bin/hermes-teacher --profile /path/to/profile assets check --vault /path/to/vault --run /path/to/run --bundle bundle.json
bin/hermes-teacher --profile /path/to/profile assets apply --vault /path/to/vault --run /path/to/run --bundle bundle.json
```

Automatic planning makes up to two requests. The second includes format or coverage errors from the first. JSON requests are limited to 120,000 bytes. For larger snapshots, retain the snapshot and plan chapters manually. After an interrupted write, apply the same plan to the same run.

## Incremental organization and relocation

Teaching can continue in a new record during organization. Incremental organization preserves earlier chapters and handwritten notes. It reports conflicts before writing if existing files have changed. Preflight a management root relocation or topic move before copying and verifying files. Active lessons, conflicting destinations, and damaged sources block relocation. A successful move retains the source. Resume the same lesson ID in the destination vault.

```sh
bin/hermes-teacher --profile /path/to/profile assets move-topic --vault /path/to/root --topic 'TOPIC' --check
bin/hermes-teacher --profile /path/to/profile assets move-root --vault /path/to/root --destination /path/to/new-root --check
```

Omit `--check` after a successful preflight to perform the move.

## Verification

Check full source coverage, reapplying a plan, manual edit conflicts, recovery after failure, cross-chapter sources, images, and local links.
