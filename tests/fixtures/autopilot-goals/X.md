---
id: X
paths: [X.txt]
goal_ids: [G-adapter]
validation:
  commands:
    - [python3, '-c', "from pathlib import Path; assert Path('X.txt').exists()"]
  artifacts: [X.txt]
---
Independently produce X.txt containing COMMON-OK. This is only part of the product goal.
A separate task awaiting owner judgment must not prevent this task from completing.
