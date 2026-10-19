---
id: B
draft: true
depends_on:
- '00'
envelope:
  paths:
  - B.txt
  validation_profiles:
  - commands:
    - - python3
      - -c
      - from pathlib import Path; assert Path('B.txt').exists(); assert 'Fixed planning
        handoff' in Path('.autopilot-inputs/handoff/00/HANDOFF.md').read_text()
    artifacts:
    - B.txt
---
# Task B
Write B.txt with good on the first line. Follow the shared requirements.
