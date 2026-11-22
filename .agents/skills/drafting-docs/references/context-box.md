# Doc info box

An optional small box directly under a Google Doc's title that answers four questions before anyone reads the body: what state the doc is in, who owns it, when it last changed, and what it relates to.

## When the template has one

Your template (`YOUR_DOC_TEMPLATE_ID`, personalize here) is the source of truth. When a copied template already carries an info box:

- Leave it exactly as copied. Do not run `replaceAllText` on it, delete and recreate it, or restyle it.
- Keep its smart chips (people, dates, files, dropdowns) as chips. Never flatten them to plain text.
- Let the user fill it in by hand in the doc.

Check it with `documents.get` after any document-wide styling pass, and restore it from the template if a pass touched it.

## Building one when the template has none

Use this only when there is no template, or when the user asks for a box on an existing doc. Send the requests in one `batchUpdate`, re-reading indices with `documents.get` between steps that depend on them.

### 1. Insert a one-cell table under the title

```json
{"insertTable": {"rows": 1, "columns": 1, "location": {"index": AFTER_TITLE_INDEX}}}
```

### 2. Style the cell

A light fill, no visible borders, comfortable padding, top-aligned.

```json
{
  "updateTableCellStyle": {
    "tableCellStyle": {
      "backgroundColor": {"color": {"rgbColor": {"red": 0.965, "green": 0.969, "blue": 0.976}}},
      "borderLeft":   {"color": {"color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}, "width": {"magnitude": 0, "unit": "PT"}, "dashStyle": "SOLID"},
      "borderRight":  {"color": {"color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}, "width": {"magnitude": 0, "unit": "PT"}, "dashStyle": "SOLID"},
      "borderTop":    {"color": {"color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}, "width": {"magnitude": 0, "unit": "PT"}, "dashStyle": "SOLID"},
      "borderBottom": {"color": {"color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}, "width": {"magnitude": 0, "unit": "PT"}, "dashStyle": "SOLID"},
      "paddingLeft":   {"magnitude": 12, "unit": "PT"},
      "paddingRight":  {"magnitude": 12, "unit": "PT"},
      "paddingTop":    {"magnitude": 10, "unit": "PT"},
      "paddingBottom": {"magnitude": 10, "unit": "PT"},
      "contentAlignment": "TOP"
    },
    "tableRange": {
      "tableCellLocation": {"tableStartLocation": {"index": TABLE_START_INDEX}, "rowIndex": 0, "columnIndex": 0},
      "rowSpan": 1,
      "columnSpan": 1
    },
    "fields": "backgroundColor,borderLeft,borderRight,borderTop,borderBottom,paddingLeft,paddingRight,paddingTop,paddingBottom,contentAlignment"
  }
}
```

### 3. Insert the four fields

Insert at the cell's first content index (usually `TABLE_START_INDEX + 3`). Use the doc owner's name for Owner, or leave it for the user.

```json
{
  "insertText": {
    "location": {"index": CELL_CONTENT_INDEX},
    "text": "Status: Draft\nOwner: {{OWNER_NAME}}\nLast updated: YYYY-MM-DD\nRelated docs: \n"
  }
}
```

### 4. Style the text

10pt text in a dark gray, normal line spacing, and only the labels bold.

```json
[
  {
    "updateTextStyle": {
      "range": {"startIndex": CELL_CONTENT_INDEX, "endIndex": CELL_END_INDEX},
      "textStyle": {
        "fontSize": {"magnitude": 10, "unit": "PT"},
        "foregroundColor": {"color": {"rgbColor": {"red": 0.2, "green": 0.22, "blue": 0.25}}}
      },
      "fields": "fontSize,foregroundColor"
    }
  },
  {
    "updateParagraphStyle": {
      "range": {"startIndex": CELL_CONTENT_INDEX, "endIndex": CELL_END_INDEX},
      "paragraphStyle": {"lineSpacing": 115, "spaceAbove": {"magnitude": 0, "unit": "PT"}, "spaceBelow": {"magnitude": 2, "unit": "PT"}},
      "fields": "lineSpacing,spaceAbove,spaceBelow"
    }
  }
]
```

Then bold each label on its own with `updateTextStyle` `{"bold": true}` over just "Status:", "Owner:", "Last updated:" and "Related docs:".

Smart chips (a person chip for Owner, a date chip for Last updated, a file chip for Related docs) can be added by hand in the Docs UI afterwards. Do not try to fake them with plain text that looks like a chip.

## Keep it out of table styling

The info box is never a content table. Any pass that styles tables skips the first table in the doc and any 1×1 table that sits before the first `HEADING_1`.
