# Croatian bank statements → camt.053.001.08

Converts Croatian bank account statements into ISO 20022 **camt.053.001.08** XML for ERP
import, following the HUB/FINA *"Uputa za klijente – camt.053/052/054"* standard (in force
since 19.11.2023).

| Script | Bank | Input |
|---|---|---|
| `pdf_to_camt053.py` | Zagrebačka banka (ZABA) | PDF statement (`IZVADAK`, layout `PLPIZV2`) |
| `pbz_sgn_to_camt053.py` | Privredna banka Zagreb (PBZ) | signed `*.zip.sgn` envelope |

## Requirements

```bash
python3 -m pip install --user -r requirements.txt   # pdfplumber, lxml
```

## Usage — ZABA (PDF)

```bash
python3 pdf_to_camt053.py --in ./in --out ./out --xsd camt.053.001.08.xsd
```

- `--in`   folder with PDF statements (scanned **recursively**, so dated subfolders work)
- `--out`  output folder (one XML per PDF)
- `--xsd`  optional: validate each output against the official schema (`[XSD OK]`)
- `--debug` print the parsed fields per statement

Each output is named `BCS.<OIB>.<YYYYMMDD>.<NNNN>.xml` per the HUB file-naming convention.

### What it does

- Reconstructs the statement table from word **coordinates** (not naive line text), so the
  columns (counterparty / references / Duguje / Potražuje) split reliably.
- Parses account/IBAN, currency, statement number, opening & closing balances, and every
  entry (booking/value dates, counterparty, poziv-na-broj references, description, amount).
- Self-checks reconciliation (`opening − debits + credits == closing`) and cross-checks the
  `Ukupan` totals printed on the PDF before writing.
- Verified on single-entry debit, single-entry credit, and multi-entry (8 debits) statements;
  output validates against the official `camt.053.001.08.xsd`.

## Usage — PBZ (signed `.sgn`)

```bash
python3 pbz_sgn_to_camt053.py --in ./in --out ./out --xsd camt.053.001.08.xsd
```

### What it does

A PBZ `.sgn` file is **not a zip** despite the `.zip.sgn` name — it is an XML `<FileUpload>`
envelope containing a base64-encoded (and internally **nested**) ZIP, plus a detached
signature and signer certificate:

```
*.zip.sgn  →  <FileUpload>
               ├─ <File compress="zip" encoding="base64">  →  ZIP → nested ZIP → camt.053 XML
               └─ <Certificate> + <Sign>                       (detached signature)
```

The payload is already camt.053 — but PBZ issues **camt.053.001.02** (ISO 2009), so the
script upconverts it to `.08`. Four changes, all verified against the official XSD:

| | `.02` | `.08` |
|---|---|---|
| namespace | `…camt.053.001.02` | `…camt.053.001.08` |
| entry status | `<Sts>BOOK</Sts>` | `<Sts><Cd>BOOK</Cd></Sts>` (`EntryStatus1Choice`) |
| related parties | `<Dbtr><Nm>` | `<Dbtr><Pty><Nm>` (`Party40Choice`) |
| financial institution | `<BIC>` | `<BICFI>` |

Also reconciles `OPBD`/`CLBD` against the entries, and is **idempotent** — re-running on `.08`
input returns it byte-identical. Output uses the unprefixed default namespace (prefixed output
is schema-valid but trips naive ERP parsers).

> The envelope's digital signature is **not verified** — only the payload is extracted.
> Treat provenance as you would the original file from the bank.

## Notes / limitations

- Tuned to the ZABA **PLPIZV2** layout. Other banks/layouts would need their own column bands.
- Counterparty `Nm` currently absorbs the address lines (the layout has no clean name/address
  boundary) — fine for IBAN-based matching.
- `BkTxCd` is generic best-effort (`PMNT` / `MDOP`|`MCOP` / `NTAV`).
- **Multi-page** statements are not yet verified (pages are read in order, but untested).
- `in/` and `out/` are git-ignored — they hold sensitive account data.
