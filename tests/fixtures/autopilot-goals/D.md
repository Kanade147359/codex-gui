---
id: D
depends_on: [C]
paths: [D.txt]
goal_ids: [G-local]
validation:
  commands:
  - [python3, '-c', "from pathlib import Path; assert Path('D.txt').exists()"]
  artifacts: [D.txt]
---
# Consumer

Read the independently accepted transform and pinned C.txt handoff in the sandbox.
Execute that transform on the accepted input, then write D.txt with `good`.
