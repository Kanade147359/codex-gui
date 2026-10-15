---
id: C
paths: [C.py, C.txt]
goal_ids: [G-local]
assumptions:
- id: A-json
  text: Input is JSON
acceptance_conditions:
- id: AC-good
  text: Transform the accepted input to good locally
  goal_ids: [G-local]
review_files: [input.csv]
handoff: [C.txt]
validation:
  commands:
  - [python3, '-c', "from pathlib import Path; exec(Path('C.py').read_text()); assert transform(Path('input.csv').read_text()) == 'good'"]
  artifacts: [C.txt]
---
# Assigned transform

Read the accepted input and produce `good`. The initial task assumes Input is JSON.
Write the transform in C.py and its result in C.txt. Follow COMMON.md.
