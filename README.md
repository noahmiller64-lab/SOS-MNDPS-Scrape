# SOS-MNDPS-Scrape

Resumable Minnesota Secretary of State business-search scrape for 3,308 unique
MNDPS payee names drawn from all FY20-FY26 tabs.

## Input

`sos_names_mndps_all_fy_tabs.txt` is UTF-8 plain text with one name per line in
the original first-seen workbook order. Only character-for-character duplicates
were removed after trimming leading and trailing whitespace.

## Pilot

```powershell
python .\scrape_mndps_sos.py `
  --limit 25 `
  --pilot-output .\MNDPS_SOS_Search_Results_Pilot_25.txt
```

The append-only `MNDPS_SOS_Search_Checkpoint.jsonl` makes the next invocation
resume at the first unsearched name.

## Full run

```powershell
.\run_mndps_scrape_background.ps1
```

Pass `-PushCheckpoints` only on a machine whose Git remote authentication is
already configured. See `MNDPS_SOS_scrape_spec.md` for the authoritative search,
capture, reliability, and output requirements.
