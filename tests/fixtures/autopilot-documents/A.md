---
id: A
draft: true
depends_on:
- '00'
envelope:
  paths:
  - A.txt
  validation_profiles:
  - commands:
    - - python3
      - -c
      - from pathlib import Path; assert Path('A.txt').exists(); assert 'Fixed planning
        handoff' in Path('.autopilot-inputs/handoff/00/HANDOFF.md').read_text()
    artifacts:
    - A.txt
---
# Task A
Write A.txt with good on the first line. Follow the shared requirements.
