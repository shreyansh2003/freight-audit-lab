# Dispute summary: Carrier F Trucking (CARF)

Period: shipments 2025-01-01 to 2025-12-31; audit as of 2026-03-31.
Audited 736 invoices; 218 flagged. Total recoverable estimate: $48,050.53. All dollar figures are estimates.

## By error type

An invoice with several flags is counted under each type.

| Error type | Flagged invoices | Flagged $ (estimate) | Recoverable $ (estimate) |
|---|---:|---:|---:|
| duplicate_invoice | 6 | $14,286.68 | $14,286.68 |
| phantom_invoice | 8 | $19,269.51 | $19,269.51 |
| rate_overcharge | 14 | $4,500.84 | $4,500.84 |
| fsc_mismatch | 190 | $8,568.50 | $8,568.50 |
| unauthorized_accessorial | 8 | $1,425.00 | $1,425.00 |

## Top 10 invoices by recoverable estimate

1. **0600346** (PRO 371551711, BOL 68917728, shipped 2025-05-23, received 2025-07-11): recoverable estimate $5,691.37. duplicate_invoice: Duplicate of invoice 0600267 (same shipment or BOL, total within tolerance): total $5,691.37 vs $5,691.37, received 32 days after the first copy (2025-06-09)
2. **0600668** (PRO 273549726, BOL 13154664, shipped 2025-11-22, received 2025-12-09): recoverable estimate $4,937.69. phantom_invoice: No shipment matches BOL 13154664 (Columbus → Salt Lake City, shipped 2025-11-22, 28,734 lb); whole invoice $4,937.69 in question
3. **0600652** (PRO 488184502, BOL 97296995, shipped 2025-11-11, received 2025-12-03): recoverable estimate $3,556.90. phantom_invoice: No shipment matches BOL 97296995 (Dallas → Pittsburgh, shipped 2025-11-11, 18,155 lb); whole invoice $3,556.90 in question
4. **0600685** (PRO 154532683, BOL 49785602, shipped 2025-11-20, received 2025-12-19): recoverable estimate $2,927.74. duplicate_invoice: Duplicate of invoice 0600648 (same shipment or BOL, total within tolerance): total $2,927.74 vs $2,927.74, received 22 days after the first copy (2025-11-27)
5. **0600249** (PRO 074408453, BOL 52392589, shipped 2025-05-17, received 2025-05-26): recoverable estimate $2,771.16. phantom_invoice: No shipment matches BOL 52392589 (Dallas → Phoenix, shipped 2025-05-17, 29,928 lb); whole invoice $2,771.16 in question
6. **0600065** (PRO 529482178, BOL 96716297, shipped 2025-02-18, received 2025-03-28): recoverable estimate $2,174.46. duplicate_invoice: Duplicate of invoice 0600065 (same shipment or BOL, total within tolerance): total $2,174.46 vs $2,174.46, received 30 days after the first copy (2025-02-26)
7. **0600432** (PRO 375739073, BOL 12213671, shipped 2025-08-07, received 2025-08-19): recoverable estimate $2,122.95. phantom_invoice: No shipment matches BOL 12213671 (Atlanta → Philadelphia, shipped 2025-08-07, 32,048 lb); whole invoice $2,122.95 in question
8. **0600128** (PRO 581321281, BOL 47128363, shipped 2025-03-11, received 2025-03-29): recoverable estimate $1,977.37. phantom_invoice: No shipment matches BOL 47128363 (Columbus → Kansas City, shipped 2025-03-11, 19,941 lb); whole invoice $1,977.37 in question
9. **0600396** (PRO 443223514, BOL 64686786, shipped 2025-06-25, received 2025-07-31): recoverable estimate $1,741.96. duplicate_invoice: Duplicate of invoice 0600330 (same shipment or BOL, total within tolerance): total $1,741.96 vs $1,741.96, received 26 days after the first copy (2025-07-05)
10. **0600728** (PRO 244235832, BOL 51818422, shipped 2025-12-14, received 2026-01-10): recoverable estimate $1,535.42. phantom_invoice: No shipment matches BOL 51818422 (Columbus → New York, shipped 2025-12-14, 22,896 lb); whole invoice $1,535.42 in question

## Systemic pattern check

- Possible systemic issue: fuel surcharge billed above schedule on 98% of invoices shipped Aug-Oct 2025 (178 of 182) (on average 9.1% above expected) vs 1% across other TL carriers (one-sided binomial p < 1e-300, below the Bonferroni-adjusted threshold 2.1e-05). Request the carrier's fuel surcharge table and corrected invoices.
