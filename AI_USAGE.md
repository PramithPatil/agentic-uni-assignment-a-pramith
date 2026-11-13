# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
ChatGPT / Codex.

## What I used them for
I used Codex as a development assistant to break down the requirements, discuss the
architecture, draft much of the initial Python implementation and tests, explain the
data model and edge cases, and help validate the pipeline on generated datasets.

## What I wrote or decided myself
I chose Assignment A, set the scope, and directed development through follow-up questions
and review requests. I selected SQLite from the options allowed by the assignment and
asked for explanations of the privacy, ingestion, and reporting choices. Codex drafted
much of the code; I used the explanations and run results to build my understanding of
the solution.

## Where the AI was wrong or unhelpful
The first orphan-resolution version repeatedly scanned all pending events, which made
the small-data run slower than necessary. This was found during validation and changed
to resolve only events related to the affected phone number or call ID.

## How I verified AI-generated code
I validated the project with seven pytest tests under Python 3.15.2 and ran generated
data for seeds 42 and 314. For seed 314, drop-by-drop and combined ingestion produced
identical reports. The five reports also matched a separate in-memory reference
calculation based on the assignment rules. The repository did not include official
golden-output files.
