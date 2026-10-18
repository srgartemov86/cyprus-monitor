// Lokali webhook: status buttons on the maps write to col K of the Locations sheet.
// One deployment serves several monitors: the secret selects the spreadsheet
// (Cluj since 10.07.2026, Cyprus added 18.10.2026 as version 2 of the same deployment).
const TARGETS = {
  'cluj-lokali-99afd68796afbe97-2026': {sheet: '1NZNlx2G24Ea-zGNurKx7fTAmHgLK4tOSjtB7ScYx-7c', name: 'Locations'},
  'cyprus-lokali-4782e161ec403a61-2026': {sheet: '1PNXJg4aT5jeWYz9sqlQmE6yoz-Lvf6D8PgJFusPZCYE', name: 'Locations'},
};
const ALLOWED_COLS = [11];  // K = Status; остальные колонки только у бота и Сергея

function doPost(e) {
  try {
    const req = JSON.parse(e.postData.contents);
    const t = TARGETS[req.secret];
    if (!t) return json({ok: false, error: 'bad secret'});
    const sh = SpreadsheetApp.openById(t.sheet).getSheetByName(t.name);
    if (req.op === 'update_cells') {
      const cells = (req.cells || []).filter(c => ALLOWED_COLS.indexOf(Number(c.col)) >= 0 && Number(c.row) >= 2);
      cells.forEach(c => sh.getRange(c.row, c.col).setValue(c.value));
      return json({ok: true, updated: cells.length});
    }
    return json({ok: false, error: 'unknown op: ' + req.op});
  } catch (err) {
    return json({ok: false, error: String(err)});
  }
}

function json(o) {
  return ContentService.createTextOutput(JSON.stringify(o))
    .setMimeType(ContentService.MimeType.JSON);
}
