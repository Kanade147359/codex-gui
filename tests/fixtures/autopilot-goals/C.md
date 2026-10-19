---
id: C
paths: [C.txt]
depends_on: [A]
goal_ids: [G-adapter]
validation:
  commands:
    - [python3, '-c', "from pathlib import Path; assert Path('C.txt').exists()"]
  artifacts: [C.txt]
---
Use the accepted predecessor and produce C.txt containing COMMON-OK.
Do not consume an unaccepted candidate, even if its individual tests pass.
