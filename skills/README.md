# Runtime skills

Hermes Teacher registers five workflows with the plugin. The teacher loads teaching steps on each turn. Research, diagrams, organization, and terminal control use dedicated roles and entry points.

| Skill | Purpose |
| --- | --- |
| [teach](teach/SKILL.md) | Assess prerequisites, agree on a plan, and check understanding throughout a lesson |
| [lesson-research](lesson-research/SKILL.md) | Investigate a topic, verify facts, and return traceable evidence |
| [lesson-visuals](lesson-visuals/SKILL.md) | Create and inspect Mermaid or SVG diagrams |
| [lesson-organizer](lesson-organizer/SKILL.md) | Organize sealed lesson records and preserve source coverage |
| [classroom-control](classroom-control/SKILL.md) | Operate classrooms on a learner's behalf and hand over control |

The plugin locates each skill inside its installation directory. Research and diagram work run as separate tasks. Organization uses sealed snapshots. Terminal control uses the lesson ID and handle to target the active process.

The classroom interface defaults to English. The teaching workflow follows the learner's requested or apparent language and uses the classroom language when it is unclear.
