# Google Sheets chart (Apps Script)

The [live chart](https://docs.google.com/spreadsheets/d/1fcVnr2D4TvZEHOT95skfsjcpe_AlilVbNe3SeTN2Cm0/edit)
is a Google Sheet with a bound Apps Script project. This directory keeps a copy
of that script, [`Code.gs`](Code.gs), so the setup can be read, reviewed and
rebuilt from the repository.

The copy is not deployed from here. The script runs from the sheet's own Apps
Script project (Extensions → Apps Script in the sheet), so a change made in this
file takes effect only once it is pasted into that editor, and a change made in
the editor should be copied back here to keep the two in step.

## What it does

- **`updateFromRepo`** fetches [`data/vacancy_counts.csv`](../data/vacancy_counts.csv)
  from the raw GitHub URL of `main` and writes it to columns A:C of `Sheet1`.
  The repository is public, so no token is needed. It clears only those columns,
  which keeps the period picker beside the chart intact.
- **The period picker** in `Sheet1!F1` offers `7 днів`, `14 днів`, `30 днів` and
  `Все`. The chart does not read `Sheet1` directly: it reads the hidden sheet
  `chart_data`, whose single formula in A1 keeps the header plus the rows dated
  within the last N days of the data, or every row for `Все`. Changing the
  picker redraws the chart at once, without running the script. The window is
  counted in calendar days back from the latest date, so a day missing from the
  CSV shows as one point fewer rather than pulling in an older day.
- **`setupPeriodFilter`** builds the picker: it creates `chart_data` with that
  formula, adds the dropdown, and points the sheet's chart at `chart_data!A:C`.
  It is a one-time step. Running it again rebuilds the same setup and resets the
  picker to 30 days.

## Setting it up on a new sheet

1. Create a Google Sheet with a tab named `Sheet1`, open Extensions → Apps
   Script, and replace the default `Code.gs` with this directory's
   [`Code.gs`](Code.gs). Paste it at the top level of the file, not inside the
   default `function myFunction() {}`, or the trigger will not find the
   functions.
2. Run `updateFromRepo` once and grant the permissions it asks for (it reads an
   external URL and edits the spreadsheet). This fills `Sheet1`.
3. Insert a line chart on `Sheet1` and style it as you like. Its data range does
   not matter, because the next step replaces it.
4. Run `setupPeriodFilter` once. This adds the picker and points the chart at the
   filtered data.
5. Under Triggers, add a time-driven trigger that runs `updateFromRepo` once a
   day, at an hour after the evening report (20:00 Europe/Kyiv) has landed. The
   live sheet uses the 22:00–23:00 slot.

## When the sheet falls behind

Google's time-driven triggers are best-effort and occasionally skip a day. If the
sheet is missing the latest row while the CSV in the repository already has it,
open the Apps Script editor and run `updateFromRepo` by hand. It rewrites the
whole history, so the missing days come back at once.
