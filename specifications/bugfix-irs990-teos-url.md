# Bug Fix: IRS 990 TEOS Bulk Data URL Changed

## Problem
The IRS 990 harvester (`irs_990.py`) downloads monthly TEOS ZIP files from:
```
https://apps.irs.gov/pub/epostcard/990/xml/{year}/{year}_TEOS_XML_{month}.zip
```
This URL now returns **404**. The IRS reorganized their public data server. The harvester is stalled at zip_idx=12 (only 2019 data processed, 0% of 2020-2026).

## Evidence
- All URLs under `https://apps.irs.gov/pub/epostcard/` return 404
- Amazon S3 mirror `https://irs-form-990.s3.amazonaws.com/` is live but stores individual filings, not bulk ZIPs
- IRS info page: `https://www.irs.gov/charities-non-profits/form-990-series-downloads` (200)

## Task
1. Find the new TEOS bulk XML ZIP URL on IRS.gov or alternative source
2. Update the `TEOS_ZIPS` URL construction in `irs_990.py` 
3. Test that a sample ZIP downloads successfully
4. Verify cursor advancement works after the fix

## Files
- `C:\Users\johnk\.hermes\agents\irs-990\scripts\irs_990.py` — agent script
- `C:\Users\johnk\AppData\Local\hermes\scripts\irs990_wrapper.py` — wrapper

## Source Data
The IRS TEOS (Tax Exempt Organization Search) bulk data contains ~12K XML filings per ZIP file, each with:
- BusinessName, EIN (organization)
- OfficerDirTrstKeyEmplGrp: PersonNm, TitleTxt, CompensationAmt (officers/directors)

## Current Cursor
- Source: `irs_990`, cursor_key: `zip_idx`, cursor_value: `12`
- 12 of 96 monthly ZIPs processed (2019 only)
