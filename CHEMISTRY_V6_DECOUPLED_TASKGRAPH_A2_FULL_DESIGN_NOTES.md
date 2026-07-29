# Chemistry V6 decoupled task-graph A2-full

## Purpose

A2-short remains the architecture ablation. A2-full changes only the prompt
calibration layer and adds audit-only structural flags. It keeps the same two
independent calls, strict schemas, image/text input, cache transport and no
automatic level changes.

This separates two questions that A2-short conflated:

1. does task-first two-call isolation help;
2. how much chemistry calibration detail is required to preserve teacher-label
   alignment?

## What was transferred from physics

- longitudinal dependency depth and whole-question task burden are separated;
- low-structure questions receive explicit negative boundary tests;
- a natural chain inside one explicit model is compressed;
- independent options cannot be accumulated into a reasoning chain;
- adjacent boundaries are decided once, with explicit lower/higher vetoes;
- postprocessing candidates require joint observable evidence rather than one
  keyword or one feature.

The physics feature schema and subject-specific keyword rules were not copied.
Chemistry uses validated task nodes, true directed edges, shared models and
chemistry-specific protocols.

## Prompt composition

The full prompts compose over the committed A2-short contracts. This avoids
duplicating and silently drifting the JSON schema while keeping A2-short
unchanged as a control.

- task reconstruction adds eight level-blind graph examples and an eight-item
  graph self-check;
- rating adds D/B separation, four adjacent-boundary tables, eight chemistry
  protocols, eighteen teacher-format examples and a final self-check;
- the short rating examples are replaced, not duplicated.

## Postprocessing policy

The first A2-full experiment is audit-only. It flags:

- low-structure overrating candidates;
- hard ratings without a decisive task edge;
- final ratings without sufficient coupling, excluding the controlled fixed-
  group teacher anchor;
- explicit disagreement with task reconstruction.

Flags never change `difficulty_level`; therefore raw and final accuracy must be
identical. After two repeated runs and manual review, a flag may become a
narrow adjacent-level rule only if it has positive net contribution on both
teacher and reviewed labels and does not reduce repeat stability.
