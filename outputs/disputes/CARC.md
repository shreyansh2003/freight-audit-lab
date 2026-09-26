# Dispute summary: Carrier C Express (CARC)

Period: shipments 2025-01-01 to 2025-12-31; audit as of 2026-03-31.
Audited 1,671 invoices; 220 flagged. Total recoverable estimate: $51,617.78. All dollar figures are estimates.

## By error type

An invoice with several flags is counted under each type.

| Error type | Flagged invoices | Flagged $ (estimate) | Recoverable $ (estimate) |
|---|---:|---:|---:|
| duplicate_invoice | 28 | $24,261.04 | $24,261.04 |
| phantom_invoice | 15 | $11,751.87 | $11,751.87 |
| rate_overcharge | 65 | $4,758.02 | $4,758.02 |
| fsc_mismatch | 33 | $392.04 | $392.04 |
| unauthorized_accessorial | 36 | $3,990.00 | $3,990.00 |
| weight_overbilling | 51 | $6,464.81 | $6,464.81 |

## Top 10 invoices by recoverable estimate

1. **0300124** (PRO 758643400, BOL 67459836, shipped 2025-01-22, received 2025-02-22): recoverable estimate $2,984.22. phantom_invoice: No shipment matches BOL 67459836 (Atlanta → Phoenix, shipped 2025-01-22, 3,741 lb); whole invoice $2,984.22 in question
2. **0300663** (PRO 666284948, BOL 27104327, shipped 2025-05-14, received 2025-06-19): recoverable estimate $2,862.28. duplicate_invoice: Duplicate of invoice 0300516 (same shipment or BOL, total within tolerance): total $2,862.28 vs $2,862.28, received 28 days after the first copy (2025-05-22)
3. **0300243** (PRO 656475837, BOL 45143965, shipped 2025-03-09, received 2025-04-21): recoverable estimate $2,182.85. duplicate_invoice: Duplicate of invoice 0300243 (same shipment or BOL, total within tolerance): total $2,182.85 vs $2,182.85, received 32 days after the first copy (2025-03-20)
4. **0300410** (PRO 102594253, BOL 56239987, shipped 2025-03-01, received 2025-05-01): recoverable estimate $2,161.44. duplicate_invoice: Duplicate of invoice 0300224 (same shipment or BOL, total within tolerance): total $2,161.44 vs $2,161.44, received 44 days after the first copy (2025-03-18)
5. **0301626** (PRO 581078397, BOL 77607388, shipped 2025-11-24, received 2026-01-06): recoverable estimate $2,059.93. duplicate_invoice: Duplicate of invoice 0301456 (same shipment or BOL, total within tolerance): total $2,059.93 vs $2,059.93, received 29 days after the first copy (2025-12-08)
6. **0301631** (PRO 983463456, BOL 73920488, shipped 2025-12-29, received 2026-01-07): recoverable estimate $1,507.84. phantom_invoice: No shipment matches BOL 73920488 (Dallas → Pittsburgh, shipped 2025-12-29, 2,820 lb); whole invoice $1,507.84 in question
7. **0301573** (PRO 878186097, BOL 95132768, shipped 2025-12-11, received 2026-01-05): recoverable estimate $1,331.82. duplicate_invoice: Duplicate of invoice 0301573 (same shipment or BOL, total within tolerance): total $1,331.82 vs $1,331.82, received 10 days after the first copy (2025-12-26)
8. **0300490** (PRO 799886174, BOL 74145898, shipped 2025-04-28, received 2025-05-17): recoverable estimate $1,251.63. phantom_invoice: No shipment matches BOL 74145898 (Atlanta → Minneapolis, shipped 2025-04-28, 2,642 lb); whole invoice $1,251.63 in question
9. **0300760** (PRO 362769781, BOL 25750641, shipped 2025-06-28, received 2025-08-11): recoverable estimate $1,133.25. duplicate_invoice: Duplicate of invoice 0300760 (same shipment or BOL, total within tolerance): total $1,133.25 vs $1,133.25, received 33 days after the first copy (2025-07-09)
10. **0300944** (PRO 115228492, BOL bol-24734965, shipped 2025-08-09, received 2025-08-24): recoverable estimate $1,128.98. weight_overbilling: Billed weight 8,600 lb vs reference 7,258 lb (shipment weight, no reweigh certificate): +18.5%; linehaul at billed weight $5,820.48 vs $4,912.21, est. +$1,128.98 with fuel surcharge

## Systemic pattern check

- Possible systemic issue: billed weight above the reference weight on 5% of invoices shipped Jan-Mar 2025 (16 of 346) (on average 20.2% above expected) vs 1% across other LTL carriers (one-sided binomial p = 1.6e-05, below the Bonferroni-adjusted threshold 2.1e-05). Request the weight and inspection documents.
