# Entity label design QA

- Reference: `codex-clipboard-f08be331-90a3-45cd-8dee-a4bf139b2364.png`
- Implementation state: local graph at 1280 × 720 with the 72-node public fixture
- Comparison: the reference and implementation screenshots were normalized into one side-by-side image before review

## Checks

- Labels remain visually adjacent to their entity nodes.
- The initial view exposes a useful subset of names instead of all labels at once.
- Light text plus a dark outline remains legible over nodes and edges.
- Zooming in increases visible labels from 24 to 48 in the 72-node fixture.
- Selecting an entity reveals and emphasizes its label even when it was hidden by the density rule.
- Dragging an entity moved its node and label by the same 64 px × -30 px delta, with no console error.
- A continuous 891 ms drag (longer than the 700 ms post-release settling window) reached the pointer's final coordinates exactly; the node stopped only after release.
- The existing graph controls, filters, and dark visual language remain unchanged.
- Frontend tests, TypeScript checks, and the production build pass.

final result: passed
