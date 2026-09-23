# Root M0 rules: official-source review

Status: source comparison for implementation; **not human verification or sign-off**. Reviewed 2026-09-23 against Leder Games' [*The Law of Root*, October 13, 2025](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756), linked as the October 2025 Law on its [official resources page](https://ledergames.com/pages/resources). This is a dated PDF; the candidate's live rules-library URL does not pin that PDF revision.

## Relevant official rules

| Section | Finding for a local Move decision |
| --- | --- |
| [1.1.1](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=2) | Cards can override the Law; applicable faction rules prevail over incompatible general rules. A base movement excerpt alone cannot establish complete legality. |
| [2.1, 2.1.1](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=3) | Section 2.1 is **Cards**, not clearings. Bird cards can substitute for another suit. |
| [2.2, 2.2.1, 2.3](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=3) | Clearings are connected by paths; two clearings joined by a path are adjacent. Rivers are not paths without an explicit rule saying otherwise. There is no standalone `2.2 Path` rule in this revision. |
| [2.5](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=3) | Rule compares each player's total warriors and buildings. Tokens and pawns do not add to that total; an ordinary tie leaves the clearing unruled. |
| [4.2, 4.2.1](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=5) | A Move takes at least one of the player's warriors and/or pawns along a linking path to an adjacent clearing; the player must rule the origin, destination, or both, subject to applicable exceptions. |
| [6.5, 6.5.2](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=6) | The Marquise obtains moves through the Daylight March action, which grants up to two Moves. A valid local movement geometry check does not establish that a March action is available. |
| [7.2.2](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=6) | The Eyrie also rule when tied for the highest warrior-plus-building total **and** they have at least one Eyrie piece in the clearing. A roost is not required by this rule. |
| [7.4.2, 7.5.2, 7.5.2.II](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=7) | The Eyrie add cards to the Decree in Birdsong, at most one new bird card. In Daylight they resolve Decree columns in order. For a Move-column card they must move at least one warrior **from** a clearing matching that card's suit. With a bird card, the origin may be of any ordinary clearing suit via §2.1.1; the destination's suit is not the Decree restriction. Failure to complete the action causes turmoil under §7.7. |

## Discrepancies found in the original candidate package

1. `root-2.1` assigns clearing/path text to official §2.1, which is about cards. `root-2.2` labels §2.2 as **Path**; the actual heading is **Clearings and Paths**, and adjacency is §2.2.1. These IDs, section fields, `source_content`, and checksums need revision before claiming source fidelity. [Official §§2.1–2.2.1](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=3)
2. `root-2.5` omits the ordinary-tie outcome and the exclusion of tokens/pawns. Its wording also changes the source's comparison by **player** to a comparison by **faction**; those are not interchangeable in every scenario. [Official §2.5](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=3)
3. `root-4.2` omits the positive piece count, the player's own pawns, and travel **on a linking path**. `root-4.2.1` captures the ordinary rule prerequisite in outline, but calling its relation to §4.2 `exception_to` is wrong: it is a prerequisite. Tagging general §§4.2–4.2.1 as inherently `daylight` also overstates the Law; phase authorization comes from faction actions, not these sections. [Official §§4.2–4.2.1](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=5)
4. `root-7.2.2` incorrectly makes roosts the basis of Eyrie rule. Replace it with the tie-for-highest plus one-Eyrie-piece condition, and represent its relation to the general tie rule as a faction exception. The existing `clarifies` relation undersells the behavioral difference. [Official §§2.5, 7.2.2](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=6)
5. The package has no §2.1.1 bird-wild rule, §7.5.2.II Decree Move rule, or §6.5.2 March authorization. These matter to the declared Marquise/Eyrie movement scope; in particular, applying a Decree card's suit to the **destination** would be incorrect. `7.5.2` is the overall Resolve the Decree section and `7.5.2.II` is its Move clause. [Official §§2.1.1, 6.5.2, 7.5.2.II](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=7)
6. The title/`scope_strategy.name` says “battle excerpts,” but the package has no battle rule and lists only `move`/`rule` actions. Remove that claim if battle is outside M0. The `coverage_obligations` currently check only adjacency and rule; they do not cover movable-piece presence, faction action authorization, Decree origin-suit matching, or exceptions. Treat these as scope limits or add rules/evidence for them before asserting full Move legality. [Official §§1.1.1, 4.2, 6.5.2, 7.5.2.II](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=7)

The candidate explicitly labels its excerpts unverified. This review supplies source locations and proposed corrections; it does not convert the package or any Golden Case to verified status. A separate domain reviewer must confirm transcription, interpretation, coverage, and case answers against the pinned Law before formal scoring.

## Prepared changes for independent review

The local draft `examples/root-m0-candidate-package.json` now pins the dated official PDF, separates §2.2.1 adjacency from §2.2.2 clearing suit, adds §2.1.1 Bird cards and §7.5.2.II Decree Move, corrects the §2.5 and §7.2.2 rule summaries, and records §6.5.2 as a boundary on Marquise action availability. The matching rule IDs in the domain decision, adapter, fixed workflow, and tests have been updated. Its `checksum` hashes the **candidate paraphrase text** in `source_content`; it is not a checksum of the official PDF. Import and tests establish schema/implementation consistency, not independent semantic approval.

`examples/m0-candidate-cases.json` is version `m0-candidate-v3`, with 13 draft Cases and no verified or disputed Cases. Candidate corrections retain their old values in `history`:

| Case | Prepared correction | Human review focus |
| --- | --- | --- |
| `ordinary-partial-01` | Complete label `INSUFFICIENT_INFORMATION` → `ILLEGAL`. | The complete A-neighbor list contains C only, so B has no path from A. Confirm that the list's completeness scope is valid. |
| `correction-legal-01` | Initial and complete `LEGAL` → `INSUFFICIENT_INFORMATION`. | The adjacency correction does not establish available warriors or rule of either endpoint. |
| `eyrie-decree-unsupported-01` | Initial and complete `UNRESOLVED` → `INSUFFICIENT_INFORMATION`. | Missing phase/card context is askable within the declared Eyrie Decree Move scope. |
| `eyrie-bird-legal-01` | Added draft Bird-card candidate. | Confirm the two-faction premise, Eyrie 3–3 tie, Bird substitution at the fox origin, and local-scope verdict. |
| `unsupported-interaction-01` | Added draft Marquise Decree candidate. | Confirm this is an unsupported faction/action combination rather than an ordinary March Move. |

The `ordinary-legal-01` and `eyrie-legal-01` inputs now state a two-player, no-exception premise; their complete labels still depend on the promised supplemental piece counts and actual completeness of those facts. A reviewer should verify the premise and sources, revise labels if it fails, or mark the Case disputed. Other draft Cases also need per-case review of initial/complete labels, permitted clarifications, and rule evidence. No candidate label is formal truth.

A **withdrawal** Case remains missing. The current evaluator fixture has one `initial_input` and a fact responder; a genuine withdrawal scenario needs an initial assertion followed by a later retraction, with both revisions and their effects preserved. A single sentence mentioning withdrawal must not be counted as this coverage. The reviewer should require a replayable multi-turn fixture or document a reasoned waiver in the detached human signoff.

Before approval, a Root reviewer must inspect each paraphrase against the pinned Law and confirm the scope/coverage obligations do not encode a Case answer or investigation route. For formal scoring, a separate reviewer must add per-Case provenance, labels, review records and checks; resolve or isolate disputes; create a family-level development/holdout split; and sign the exact dataset digest with a protected key. The candidate and this note provide no review identity, approval, or signoff.

## Second AI audit, 2026-09-23

A separate AI agent independently compared the current draft rules and all 13 Case labels with the same pinned official Law. It reported no substantive conflict in the rule paraphrases or local-scope labels. It did find underspecified question sets and a conditionally applicable Eyrie rule obligation. The candidate was revised as follows:

- `ordinary-legal-01`, `eyrie-legal-01`, and `eyrie-bird-legal-01` now put the two-player and no-exception premises in their public input. The Eyrie example also offers the missing Marquise building count before its complete `LEGAL` label.
- `missing-fact-01` now supplies separately stated A-clearing piece counts, leaving the path as the intended missing local condition.
- `adversarial-prompt-01` now allows questions about move count, movable warriors, adjacency, and endpoint rule. `eyrie-decree-unsupported-01` now allows path and piece/rule questions in addition to Decree context. `correction-legal-01` now states the two-player/no-exception premise publicly.
- The package separates generic movement-piece/rule coverage from the Eyrie highest-count tie exception; the Bird relation is explicitly noted as conditional on a Bird card.

This was **AI peer review**, not human verification. It has no human reviewer identity, cannot independently attest the source or labels, and must not change any `draft` status. The evaluator still cannot replay a true multi-message withdrawal Case from one `initial_input`; that coverage requires a replay-model change or a documented human waiver. The formal scoring gate remains closed.
