---
id: D
depends_on:
- I1
- '00'
paths:
- D.txt
validation:
  commands:
  - - python3
    - -c
    - from pathlib import Path; assert Path('D.txt').exists(); assert 'COMMON-OK'
      in Path('.autopilot-inputs/handoff/I1/wiring.py').read_text()
  artifacts:
  - D.txt
---
# Successor
Read the integrated code and accepted handoffs, then write D.txt.
