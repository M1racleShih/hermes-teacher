---
name: lesson-visuals
description: Create, inspect, and publish Mermaid or SVG diagrams for a lesson.
version: 0.1.0
---

# Lesson diagrams

Use Mermaid for dependencies, workflows, and structural relationships. Use SVG for geometry, vectors, number lines, coordinates, and precise positions. Deliver the source, inspected render, published path, and classroom reference.

Start a `mermaid` or `svg` task with `teacher_role`. Include the concept, labels, and relationships. Keep the task `id` and use `wait` for the result or `status` for a preview. Send requested changes to the original task with `supplement`. Embed the published diagram with `result.asset.embed`. The same reference is included in the lesson record.

## Workflow

1. Identify the main concept and the objects and relationships it needs.
2. Choose the matching role and specify the goal, relationships, labels, dimensions, and teaching language. The tool resolves the vault from the current lesson.
3. Create the source and render a preview. Check direction, geometry, label readability, contrast, and cropping.
4. Open the actual PNG to approve it or revise it. Render and inspect each revision.
5. Publish the approved PNG and source to `viz/` in the current vault. Report the vault-relative path and preview status.
6. Include the published image in the lesson record. A supplemented task creates a new version and preserves earlier references.

Use short labels in the teaching language and explain specialized terms in the lesson. Keep the source, temporary preview, and published files separate. Each role uses its own tools and context. If rendering or inspection fails, report the issue and what needs correction.

## Verification

Check that the file opens, addresses the learning objective, has readable labels and correct geometry or relationships, and is published in the lesson vault.
