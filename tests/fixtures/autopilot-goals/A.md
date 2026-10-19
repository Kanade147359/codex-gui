---
id: A
paths: [A.txt]
goal_ids: [G-adapter]
acceptance_ids: [AC-A]
validation:
  commands:
    - [python3, '-c', "from pathlib import Path; assert Path('A.txt').exists()"]
  artifacts: [A.txt]
---
Task assumption: adapter-v1 is available. Deliver A.txt with COMMON-OK.
If capability.txt disproves that assumption, report the observation and counterexample.
The deliverable contract is version independent; use the supported adapter to deliver it.
