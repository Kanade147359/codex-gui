---
id: C
draft: true
depends_on:
- '00'
envelope:
  paths:
  - C.txt
  validation_profiles:
  - commands:
    - - python3
      - -c
      - from pathlib import Path; assert Path('C.txt').exists(); assert 'Fixed planning
        handoff' in Path('.autopilot-inputs/handoff/00/HANDOFF.md').read_text()
    artifacts:
    - C.txt
---
# Task C
Write C.txt with good on the first line. Follow the shared requirements.
