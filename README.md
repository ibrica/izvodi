# ZABA PDF → camt.053 converter

Converts Zagrebačka banka (ZABA) PDF account statements (`IZVADAK`, internal layout
`PLPIZV2`) into ISO 20022 **camt.053.001.08** XML for ERP import. Follows the Croatian
HUB/FINA *"Uputa za klijente – camt.053/052/054"* standard (in force since 19.11.2023).

## Requirements

```bash
python3 -m pip install --user -r requirements.txt   # pdfplumber, lxml
```

## Usage

```bash
python3 pdf_to_camt053.py --in ./in --out ./out --xsd camt.053.001.08.xsd
```

- `--in`   folder with PDF statements (scanned **recursively**, so dated subfolders work)
- `--out`  output folder (one XML per PDF)
- `--xsd`  optional: validate each output against the official schema (`[XSD OK]`)
- `--debug` print the parsed fields per statement

Each output is named `BCS.<OIB>.<YYYYMMDD>.<NNNN>.xml` per the HUB file-naming convention.

## What it does

- Reconstructs the statement table from word **coordinates** (not naive line text), so the
  columns (counterparty / references / Duguje / Potražuje) split reliably.
- Parses account/IBAN, currency, statement number, opening & closing balances, and every
  entry (booking/value dates, counterparty, poziv-na-broj references, description, amount).
- Self-checks reconciliation (`opening − debits + credits == closing`) and cross-checks the
  `Ukupan` totals printed on the PDF before writing.
- Verified on single-entry debit, single-entry credit, and multi-entry (8 debits) statements;
  output validates against the official `camt.053.001.08.xsd`.

## Notes / limitations

- Tuned to the ZABA **PLPIZV2** layout. Other banks/layouts would need their own column bands.
- Counterparty `Nm` currently absorbs the address lines (the layout has no clean name/address
  boundary) — fine for IBAN-based matching.
- `BkTxCd` is generic best-effort (`PMNT` / `MDOP`|`MCOP` / `NTAV`).
- **Multi-page** statements are not yet verified (pages are read in order, but untested).
- `in/` and `out/` are git-ignored — they hold sensitive account data.
