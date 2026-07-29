# Chemistry V6 decoupled task-graph A2 design notes

## Why A2 exists

A1 correctly removed Evidence-15 from primary rating and automatic
postprocessing, but it still generated the level and graph in one call and
asked for the level first. The external review is right that this is field-use
isolation, not reasoning-process isolation, and that a graph written after the
level can become a post-hoc justification.

A2 preserves both V6 and A1 as immutable controls. It is a new two-call
ablation:

1. pass 1 reconstructs all question tasks and emits a graph without seeing any
   of the five level names or the rating rubric;
2. the program validates the graph and derives audit metrics;
3. pass 2 receives the original question and validated reconstruction, then
   assigns a level under the teacher rubric;
4. the program derives neighboring level names but never changes the level.

Separate prompt prefixes and cache IDs ensure pass 2 is not a continuation of
the pass-1 response and pass 1 cannot condition on the desired level.

## Accepted changes from the review

- Full stem/subquestion/option/diagram/flow/table/curve reconstruction is
  restored before rating.
- The rated target and its indispensable prerequisites are explicit.
- Shared models are undirected objects, not fake `model_dependency` edges.
- `conclusion` nodes are removed. Pure arithmetic, final-answer wording and
  rule selection without application are not nodes.
- Node granularity, merge rules, a primary-kind priority and
  `counts_as_decision` are defined.
- The 20-node limit is advisory; the hard schema maximum is 30.
- A compact enum audit layer is retained for repeat-run stability analysis,
  but it is prohibited from deciding or postprocessing the level.
- Detailed chemistry protocols for reaction order/excess, experiment and
  evidence, graphs/tables, quantitative models, transfer and representations
  are restored as internal decision protocols rather than Evidence-15 output.
- Adjacent veto rules and ten concrete examples replace abstract templates.
- The complete unreconstructable JSON contract is specified.
- Neighboring level names are program-derived, eliminating an unstable model
  field.

## Qualified agreement and remaining differences

### A graph must precede rating, but it is still not an oracle

The review is correct about order. A2 therefore constructs the graph in a
separate first call. It would still be wrong to treat the graph as objective
ground truth: pass 1 can omit or invent a task. Graph metrics remain audit
signals only and may not become level thresholds without repeated validation.

### Structured audit labels are useful, but cannot be independent votes

The review is right that all-free-text dimensions prevent repeatability
measurement. A2 uses small enums. We do not accept that enum structure alone
makes the concepts independent: the same chemical fact may legitimately
appear in more than one dimension, so the prompt explicitly forbids counting,
averaging or repeated voting.

### The teacher special anchor is retained only for label replication

A1 rejected named exceptions because they reduce theoretical generality. The
new review correctly distinguishes two targets. This project currently
measures agreement with existing teacher labels, so A2 includes the one known
fixed-sulfate-group anchor under a strict true-isomorphism condition and emits
an audit flag. It is explicitly not a universal chemistry-difficulty rule. A
theory-standard evaluation should run the same outputs against reviewed labels
with this exception removed or relabeled.

### More detail is not automatically better

We restore protocols and negative boundary examples, not the entire old
Evidence-15 production contract. We continue to reject feature counts,
single-feature level triggers, forced feature-level consistency,
`coarse_difficulty`, duplicated examples and automatic rules in the first A2
experiment.

## Experiment interpretation

A2 costs roughly two model calls per question, so accuracy alone is
insufficient. Compare V6, A1 and A2 on the same 591 items using:

- raw/strict accuracy against both original and reviewed labels;
- failures and unreconstructable counts by stage;
- repeat-run level agreement;
- task graph, audit attribute and rated-target stability;
- confusion pairs at every adjacent boundary;
- graph-review disagreement precision under manual review;
- runtime and token cost.

No graph-derived postprocessor should be introduced until A2 has at least two
full repeats and a candidate rule shows positive net contribution on reviewed
evidence without degrading stability.
