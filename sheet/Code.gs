const CSV_URL = 'https://raw.githubusercontent.com/eugene-jet/job-searcher/main/data/vacancy_counts.csv';
const SHEET_NAME = 'Sheet1';
const CHART_DATA_SHEET = 'chart_data';
const PERIOD_CELL = 'F1';
const PERIODS = ['7 днів', '14 днів', '30 днів', 'Все'];

function updateFromRepo() {
  const resp = UrlFetchApp.fetch(CSV_URL, { muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) {
    throw new Error('CSV fetch failed: HTTP ' + resp.getResponseCode());
  }
  const rows = Utilities.parseCsv(resp.getContentText());
  if (rows.length < 1) return;

  const out = rows.map(function (r, i) {
    return i === 0 ? ['date', 'DOU', 'Djinni'] : [r[0], toNum(r[1]), toNum(r[2])];
  });

  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  // Clear only the data columns, so the period picker next to the chart survives.
  sheet.getRange('A:C').clearContent();
  sheet.getRange(1, 1, out.length, 3).setValues(out);
  // The chart reads the chart_data sheet, whose formula follows these rows on its own.
}

// One-time setup of the period picker above the chart. Safe to run again:
// it rebuilds chart_data and resets the picker to 30 days.
function setupPeriodFilter() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(SHEET_NAME);
  const data = ss.getSheetByName(CHART_DATA_SHEET) || ss.insertSheet(CHART_DATA_SHEET);

  // Header plus the rows whose date falls within the last N days of the data;
  // a period without a number in it ("Все") keeps every row.
  data.clear();
  data.getRange('A1').setFormula(
    '=LET(' +
    'n, IFERROR(VALUE(REGEXEXTRACT(' + SHEET_NAME + '!' + PERIOD_CELL + ', "[0-9]+")), 100000), ' +
    'd, FILTER(' + SHEET_NAME + '!A2:C, ' + SHEET_NAME + '!A2:A <> ""), ' +
    'dates, INDEX(d, 0, 1), ' +
    '{' + SHEET_NAME + '!A1:C1; FILTER(d, dates > MAX(dates) - n)})'
  );
  data.getRange('A:A').setNumberFormat('yyyy-mm-dd');
  data.hideSheet();

  sheet.getRange('E1').setValue('Період:');
  sheet.getRange(PERIOD_CELL)
    .setDataValidation(SpreadsheetApp.newDataValidation()
      .requireValueInList(PERIODS, true)
      .setAllowInvalid(false)
      .build())
    .setValue('30 днів');

  const chart = sheet.getCharts()[0];
  sheet.updateChart(chart.modify()
    .clearRanges()
    .addRange(data.getRange('A:C'))
    .setPosition(3, 5, 0, 0)
    .build());
}

function toNum(v) {
  return (v === '' || v == null) ? '' : Number(v);
}
