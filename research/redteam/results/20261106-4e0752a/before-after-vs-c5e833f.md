# R1 before/after: c5e833f vs 4e0752a

Totals are caught / neutralised / bounded / not caught.

| Run | Cases | Totals |
|---|---|---|
| c5e833f | 79 | 72 / 3 / 0 / 4 |
| 4e0752a | 79 | 73 / 3 / 0 / 3 |
| 4e0752a, rows also in c5e833f | 79 | 73 / 3 / 0 / 3 |

## Rows that changed (1)

| ID | Threat | Before | After | Fixed by | After evidence |
|---|---|---|---|---|---|
| A5 | T6 | not caught (none) | caught (code) | F7 | auto: stop-loss fired -> executed ('submitted'), the position was sold, rule now triggered; human: stop-loss queued and the operator's approval sold it (submitted=True) |

## New rows (0)

| ID | Threat | Attack | Result | Fixed by |
|---|---|---|---|---|

## Still not caught or bounded in 4e0752a (3)

| ID | Threat | Result | Residual |
|---|---|---|---|
| X2 | T2 | not caught (none) | gap |
| M2 | T3 | not caught (none) | gap |
| M6 | T3 | not caught (none) | design |
