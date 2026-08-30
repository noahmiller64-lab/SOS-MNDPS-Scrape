# TASK: Scrape Minnesota Secretary of State business search results for 3,308 MNDPS payee names

You are to write and run a script that searches the Minnesota Secretary of State
business search portal for every name in
`sos_names_mndps_all_fy_tabs.txt` (one name per line, 3,308 lines). The source is
the MNDPS FY20-FY26 workbook, in first-seen order across all FY tabs. Only
character-for-character duplicate payee names were removed after trimming leading
and trailing whitespace; the fixed order must not be changed. Extract the business
details for the matches and produce the final PDF report in the exact format
described in section 5. Run it start to finish without sampling or shortcuts.

## 1. The portal

- Search page: https://mblsportal.sos.mn.gov/Business/Search
- Business detail pages look like:
  https://mblsportal.sos.mn.gov/Business/SearchDetails?filingGuid=<guid>
- This is a public government records portal. Be polite: at most 1 request per
  second (add a delay between requests), identify with a normal user-agent, retry
  a failed request up to 3 times with backoff, and if the site starts refusing
  requests, slow down rather than hammering it.

## 2. Search rules (these are the heart of the job — follow exactly)

For EVERY name, use these settings:
- Search scope: **Begins With** — NEVER "Contains". (A previous run used Contains
  and returned mountains of unrelated businesses that merely shared a word. If you
  cannot make the portal's Begins With mode work programmatically, emulate it:
  run the search however required, then KEEP ONLY results whose business name —
  current or prior — starts with the searched string, case-insensitive.)
- Include Prior Names: **Include**
- Search the name EXACTLY as written in the file — verbatim. Do not correct
  spelling, expand abbreviations (MN, SVCS, ASSOC, CTR...), or add/remove
  punctuation. Trim only leading/trailing whitespace.

Per-name sequence — stop at the first step that returns results:
  a. Begins With + Filing Status "Active", full name as written.
  b. If no results: same string, Filing Status "Inactive".
  c. If still nothing AND the name ends in a corporate suffix
     (INC, LLC, CORP, CO, LTD, LLP, LP, PA, PLLC — with or without periods/commas):
     drop that one trailing suffix and repeat steps a and b with the shortened
     string. Never shorten further than the one trailing suffix.
  d. If still nothing, record for that name:
     "No results (Begins With, Active and Inactive, suffix-drop attempted)."
     Never fall back to a Contains-style search.

## 3. What to capture when there are results

- Record the Filing Status used (Active or Inactive) and the TOTAL number of
  matches returned.
- Open the detail pages of up to the FIRST THREE matches, Active ones first.
- For each opened match capture:
  - The full detail-page URL
  - The result's status line as shown in the results list
    (e.g. "Active | Limited Liability Company (Domestic) | Minnesota Business Name")
    — and note when the hit came via a PRIOR name rather than the current name
  - EVERY information field present on the detail page. Typical fields:
    Business Type; MN Statute; File Number; Home Jurisdiction; Filing Date;
    Status; Renewal Due Date; Registered Office Address; Number of Shares;
    Registered Agent(s); Principal Executive Office Address; Chief Executive
    Officer + address; Manager + address; President; Mailing Address; Principal
    Place of Business Address; Applicant + Applicant Address (Assumed Names);
    Home Business Name. Pages differ — capture whatever appears, invent nothing,
    omit nothing that is shown.
  - The COMPLETE Filing History and Renewal History lists, including any
    "(Business Name: ...)" annotations — those contain prior names and are needed.

## 4. Reliability requirements

- Write progress incrementally to a checkpoint file (e.g. JSONL, one record per
  searched name) so that if the run is interrupted, it resumes at the next
  unsearched name instead of restarting. Keep numbering continuous.
- Never silently skip a name. Every one of the 3,308 names gets a numbered
  section in the output, even the no-results ones.
- Before the full run, do a pilot on the first 25 names, sanity-check the parsed
  output against the live site, then continue automatically if it looks right.

## 5. Deliverables

Produce BOTH:
1. `MNDPS_SOS_Search_Results_AllTabs.pdf` — formatted to match this layout exactly:
   - Document title: "Minnesota Secretary of State - Business Search Results"
   - Header block underneath:
     "Source list: MNDPS all-FY-tabs payee list (FY20-FY26), exact-text de-duplicated, 3,308 names."
     "Search rules: Scope = Begins With; Include Prior Names = Include; Filing
      Status = Active first, Inactive if no active results; suffix-drop fallback;
      first up to 3 matches opened per name."
     "Searched 3,308 names: <X> with matches, <Y> with no results (Active or Inactive)."
     "Portal: https://mblsportal.sos.mn.gov/Business/Search  Generated: <date>"
   - Then one numbered section per name, in file order, formatted like:

     1. BEHAVIORAL MED ASSOC INC
        No results match the criteria entered (searched both Active and Inactive).

     2. BEK CARE LLC
        Filing Status used: Active; 2 match(es) returned
     Match 1: BEK Care LLC
        Active | Limited Liability Company (Foreign) | Home Business Name
        https://mblsportal.sos.mn.gov/Business/SearchDetails?filingGuid=...
        Business Type: Limited Liability Company (Foreign)
        MN Statute: 322C
        File Number: ...
        (...every captured field, one per line...)
        Filing History:
           3/26/2014 - Original Filing - ... 
        Renewal History:
           10/27/2015 - Annual Renewal - ...
     Match 2: ...

   - Name headers bold/caps, match headers bold, page numbers bottom right.
2. The same content as a plain text file `MNDPS_SOS_Search_Results_AllTabs.txt`
   (this is the machine-readable master; the PDF is generated from it).

If the finished PDF is too large to hand over in one piece, split it into parts
at clean section boundaries (e.g. part 1 = names 1-2000) and deliver all parts.

## 6. Scale expectations

~3,308 names may require several thousand search requests plus up to three
detail-page requests per matched name. At one request per second this is a
multi-hour job. That is expected — checkpoint and keep going. Do not "sample" or
shortcut the list; every name must be searched.
