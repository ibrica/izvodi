# Croatian bank statements → camt.053.001.08

Converts Croatian bank account statements into ISO 20022 **camt.053.001.08** XML for ERP
import, following the HUB/FINA *"Uputa za klijente – camt.053/052/054"* standard (in force
since 19.11.2023).

| Script | Bank | Input |
|---|---|---|
| `pdf_to_camt053.py` | Zagrebačka banka (ZABA) | PDF statement (`IZVADAK`, layout `PLPIZV2`) |
| `pbz_sgn_to_camt053.py` | Privredna banka Zagreb (PBZ) | signed `*.zip.sgn` envelope |

## Setup

Needs **Python 3.9 or newer** and two packages (`pdfplumber`, `lxml`). Everything is pure
Python — there is nothing else to install (no Poppler, no Java, no Acrobat).

### macOS / Linux

Install [uv](https://docs.astral.sh/uv/) if you don't have it (`brew install uv` on macOS).
A Python installed by uv is externally managed, so `pip install` (including `--user`) is
refused. Create a project virtualenv — `.venv/` is git-ignored — and install into that:

```bash
uv venv
uv pip install -r requirements.txt
```

`uv run` uses that environment, so you don't need to activate it. The run commands below
use this form.

### Windows

1. **Install Python** from [python.org/downloads](https://www.python.org/downloads/).
   During the installer, tick **"Add python.exe to PATH"** — this is the step people miss.
   Then open **PowerShell** and check it worked:

   ```powershell
   py --version
   ```

2. **Get the code** — either `git clone <repo-url>`, or download the repo as a ZIP from
   GitHub ("Code" → "Download ZIP") and extract it.

3. **Install the dependencies** from inside the project folder:

   ```powershell
   cd C:\path\to\Izvodi
   py -m pip install -r requirements.txt
   ```

### Create the `in` folder

`in/` and `out/` are git-ignored (they hold real account data), so a fresh clone won't have
them. Create `in` and drop the statement files in — `out` is created automatically:

```bash
mkdir in          # PowerShell and bash both accept this
```

That's it — the run commands are below.

## Usage — ZABA (PDF)

Put the PDF statements in `in/`, then:

```bash
uv run python pdf_to_camt053.py --in ./in --out ./out --xsd camt.053.001.08.xsd
```

On Windows (PowerShell), use `py` and backslashes:

```powershell
py pdf_to_camt053.py --in .\in --out .\out --xsd camt.053.001.08.xsd
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

Put the `*.zip.sgn` files in `in/`, then:

```bash
uv run python pbz_sgn_to_camt053.py --in ./in --out ./out --xsd camt.053.001.08.xsd
```

On Windows (PowerShell):

```powershell
py pbz_sgn_to_camt053.py --in .\in --out .\out --xsd camt.053.001.08.xsd
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
script upconverts it to `.08`. The change set was derived by **diffing the .02 and .08
schemas**, not from one sample file, so constructs absent from any given statement (bank
charges especially) are still handled:

| | `.02` | `.08` |
|---|---|---|
| namespace | `…camt.053.001.02` | `…camt.053.001.08` |
| entry status | `<Sts>BOOK</Sts>` | `<Sts><Cd>BOOK</Cd></Sts>` (`EntryStatus1Choice`) |
| related parties | `<Dbtr><Nm>` | `<Dbtr><Pty><Nm>` (`Party40Choice`) |
| financial institution | `<BIC>` | `<BICFI>` |
| **bank charges** | `<Chrgs><Amt>` (repeatable) | `<Chrgs><Rcrd><Amt>` — one `Chrgs`, many `Rcrd`, so repeats are **merged**; `<Pty>`→`<Agt>` |
| organisation id | `<OrgId><BICOrBEI>` | `<OrgId><AnyBIC>` |
| summary net total | `<TtlNetNtryAmt Ccy=…>` | `<TtlNetNtry><Amt>` (currency attribute dropped) |
| remittance location | flat `<RmtLctnMtd>` | `<RmtLctnDtls><Mtd>` |
| tax admin zone | `<AdmstnZn>` | `<AdmstnZone>` |

Also reconciles `OPBD`/`CLBD` against the entries, and is **idempotent** — re-running on `.08`
input returns it byte-identical. Output uses the unprefixed default namespace (prefixed output
is schema-valid but trips naive ERP parsers).

> The envelope's digital signature is **not verified** — only the payload is extracted.
> Treat provenance as you would the original file from the bank.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `py : The term 'py' is not recognized` | Python isn't on PATH. Reinstall from python.org with **"Add python.exe to PATH"** ticked, or try `python` instead of `py`. |
| `No module named pdfplumber` / `lxml` | Dependencies not installed, or installed for a different Python. On macOS/Linux run `uv pip install -r requirements.txt` and start the script with `uv run python`. On Windows run `py -m pip install -r requirements.txt` (the `py -m` prefix targets the same interpreter that runs the script). |
| `externally-managed-environment` | You ran `pip install` against a uv-managed Python. Use the project virtualenv instead: `uv venv` then `uv pip install -r requirements.txt`. |
| `No PDFs found` / `No .sgn files found` | Check `--in` points at the right folder. Subfolders are scanned automatically, so `--in .\in` finds `in\Izvodi 2026-06-12\*.pdf`. |
| Folder name has spaces | Quote it: `--in ".\in\Izvodi 2026-06-12"` |
| `running scripts is disabled on this system` | That's PowerShell blocking a `.ps1`, not this tool. These are plain `py` commands — they don't need a policy change. |

Forward slashes also work on Windows (`--in ./in`), so commands can be copy-pasted from
either section. Console output is forced to UTF-8, so Croatian characters (č ć ž š đ) in
account and counterparty names survive being redirected to a file
(`py pdf_to_camt053.py --in .\in --out .\out > log.txt`).

## Notes / limitations

- Tuned to the ZABA **PLPIZV2** layout. Other banks/layouts would need their own column bands.
- Counterparty `Nm` currently absorbs the address lines (the layout has no clean name/address
  boundary) — fine for IBAN-based matching.
- `BkTxCd` is generic best-effort (`PMNT` / `MDOP`|`MCOP` / `NTAV`).
- **Multi-page** statements are not yet verified (pages are read in order, but untested).
- `in/` and `out/` are git-ignored — they hold sensitive account data.
