---
id: E
paths: [E.txt]
goal_ids: [G-local]
validation:
  commands:
  - [python3, '-c', "from pathlib import Path; assert Path('E.txt').exists()"]
  artifacts: [E.txt]
---
# Independent slice

Write E.txt with `good`. This slice must continue while C awaits a root-cause decision.
