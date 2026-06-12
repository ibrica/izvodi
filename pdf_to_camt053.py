#!/usr/bin/env python3
"""
Convert Zagrebačka banka (ZABA) PDF account statements ("IZVADAK", layout PLPIZV2)
into ISO 20022 camt.053.001.08 XML for ERP import.

This is a proof-of-concept tuned to the PLPIZV2 layout. It reconstructs the
statement table from word coordinates (pdfplumber) rather than naive line text,
which is what makes the column split (counterparty / references / debit / credit)
reliable.

Usage:
    python3 pdf_to_camt053.py --in . --out ./out [--xsd camt.053.001.08.xsd] [--debug]

Each input PDF -> one XML named  BCS.<OIB>.<YYYYMMDD>.<NNNN>.xml  (HUB spec naming).
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pdfplumber
from lxml import etree

logging.getLogger("pdfminer").setLevel(logging.ERROR)  # silence color-space warnings

CAMT_NS = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"

# ---- Column x-bands (page width ~595pt), derived from the PLPIZV2 layout. ----
# row#  | booking-date/Referenca | value-date | counterparty | poziv+opis | Duguje | Potražuje
COL_ROW_MAX = 58.0        # row number (entry start) lives left of this
COL_REF1_MAX = 145.0      # booking date (1st line) + Referenca (continuation lines)
COL_VALDATE_MAX = 212.0   # value date
COL_CPARTY_MAX = 318.0    # counterparty account / name / address
COL_REFDESC_MAX = 456.0   # poziv na broj platitelja/primatelja + opis plaćanja
DEBIT_CREDIT_SPLIT_X = 514.0  # amount center < split -> Duguje (debit), else Potražuje (credit)

DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
POZIV_RE = re.compile(r"^HR\d{2}\b")
IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{8,30}$")


# --------------------------------------------------------------------------- #
# Small value helpers
# --------------------------------------------------------------------------- #
def parse_hr_amount(text: str) -> Decimal:
    """'18.865,76' -> Decimal('18865.76'); handles optional leading '-'."""
    t = text.strip().replace(" ", "")
    neg = t.startswith("-")
    t = t.lstrip("-").replace(".", "").replace(",", ".")
    try:
        val = Decimal(t)
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {text!r}") from exc
    return -val if neg else val


def parse_hr_date(text: str) -> str:
    """'10.04.2026.' (or embedded) -> ISO '2026-04-10'."""
    m = DATE_RE.search(text)
    if not m:
        raise ValueError(f"no date in {text!r}")
    d, mo, y = m.groups()
    return f"{y}-{mo}-{d}"


def is_int_token(text: str) -> bool:
    return text.strip().isdigit()


def amt_str(d: Decimal) -> str:
    """camt amount: dot decimal, 2 places, no sign (sign carried by CdtDbtInd)."""
    return f"{abs(d):.2f}"


def poziv_ref(poziv: str) -> str | None:
    """A 'poziv na broj' carries a real reference only if something follows the
    HRxx model (e.g. 'HR01 1102...'). Bare 'HR99'/'HR00' = no reference -> None."""
    parts = poziv.split()
    return poziv if len(parts) >= 2 else None


# --------------------------------------------------------------------------- #
# Line / word reconstruction
# --------------------------------------------------------------------------- #
@dataclass
class Word:
    x0: float
    x1: float
    text: str

    @property
    def center(self) -> float:
        return (self.x0 + self.x1) / 2.0


@dataclass
class Line:
    page: int
    top: float
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in sorted(self.words, key=lambda w: w.x0))

    def col(self, x_min: float, x_max: float) -> list[Word]:
        return [w for w in sorted(self.words, key=lambda w: w.x0) if x_min <= w.x0 < x_max]

    def col_text(self, x_min: float, x_max: float) -> str:
        return " ".join(w.text for w in self.col(x_min, x_max))


def extract_lines(pdf_path: str) -> list[Line]:
    """All words across all pages -> lines grouped by (page, top) within tolerance."""
    lines: list[Line] = []
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages):
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            buckets: dict[int, list[Word]] = {}
            for w in words:
                key = round(w["top"] / 3.0)  # ~3pt tolerance
                buckets.setdefault(key, []).append(Word(w["x0"], w["x1"], w["text"]))
            for key in sorted(buckets):
                ws = buckets[key]
                top = min(w.x0 for w in ws)  # placeholder, real top below
                lines.append(Line(page=pno, top=key * 3.0, words=ws))
    lines.sort(key=lambda ln: (ln.page, ln.top))
    return lines


# --------------------------------------------------------------------------- #
# Parsed model
# --------------------------------------------------------------------------- #
@dataclass
class Entry:
    booking_date: str
    value_date: str
    amount: Decimal
    cdt_dbt: str                      # 'DBIT' or 'CRDT'
    cparty_iban: str | None = None
    cparty_name: str = ""
    refs: list[str] = field(default_factory=list)   # Referenca (AcctSvcrRef candidates)
    poziv_platitelj: str = ""         # debtor reference (poziv na broj platitelja)
    poziv_primatelj: str = ""         # creditor reference (poziv na broj primatelja)
    description: str = ""             # opis plaćanja


@dataclass
class Statement:
    account_iban: str
    currency: str
    stmt_no: str
    stmt_date: str                    # ISO
    owner_name: str
    owner_addr: list[str]
    owner_oib: str
    bank_name: str
    bank_bic: str
    opening: Decimal
    opening_date: str
    closing: Decimal
    closing_date: str
    entries: list[Entry]
    # totals as printed on the PDF (for cross-check)
    tot_dbt_count: int = 0
    tot_crd_count: int = 0
    tot_dbt_sum: Decimal = Decimal("0")
    tot_crd_sum: Decimal = Decimal("0")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _grab(lines: list[Line], pattern: str, group: int = 1, flags=0) -> str | None:
    rx = re.compile(pattern, flags)
    for ln in lines:
        m = rx.search(ln.text)
        if m:
            return m.group(group)
    return None


def parse_header(lines: list[Line]) -> dict:
    """Document-level metadata shared by all account sections."""
    full = "\n".join(ln.text for ln in lines)

    stmt_no = None
    m = re.search(r"IZVADAK\s+br\.?\s*(\d+)", full)
    if m:
        stmt_no = m.group(1)

    stmt_date = None
    for ln in lines:
        m = re.search(r"^[A-Za-zČĆŠŽĐčćšžđ]+,\s*(\d{2}\.\d{2}\.\d{4})", ln.text)
        if m:
            stmt_date = parse_hr_date(m.group(1))
            break

    bank_name = _grab(lines, r"(Zagrebačka banka[^,]*?d\.d\.)") or "Zagrebačka banka d.d."
    bank_bic = _grab(lines, r"SWIFT/BIC:\s*([A-Z0-9]{8,11})") or ""

    # Two OIBs appear: bank (left column, x<350) and owner (right column, x>=350).
    bank_oib = owner_oib = None
    for ln in lines:
        for w in ln.words:
            if w.text == "OIB:":
                # find following number on same line
                nums = [ww for ww in ln.words if ww.text.isdigit() and len(ww.text) == 11]
                near = [ww for ww in nums if ww.x0 > w.x0]
                if near:
                    val = sorted(near, key=lambda ww: ww.x0)[0].text
                    if w.x0 < 350:
                        bank_oib = bank_oib or val
                    else:
                        owner_oib = owner_oib or val

    # Owner name/address: right-column block (x>=350) just under the OIB/MB header.
    owner_lines: list[str] = []
    for ln in lines:
        right = [w.text for w in sorted(ln.words, key=lambda w: w.x0) if w.x0 >= 350]
        if not right:
            continue
        txt = " ".join(right)
        if re.search(r"IZVADAK|OIB:|MB:|PLPIZV2|^\d{6}$", txt):
            continue
        if re.match(r"^\d{6}$", txt.strip()):
            continue
        owner_lines.append(txt)
    # owner name = first plausible line; rest = address (best effort)
    owner_name = owner_lines[0] if owner_lines else ""
    owner_addr = owner_lines[1:3]

    return {
        "stmt_no": stmt_no or "0",
        "stmt_date": stmt_date or datetime.now().strftime("%Y-%m-%d"),
        "bank_name": bank_name,
        "bank_bic": bank_bic,
        "bank_oib": bank_oib or "",
        "owner_oib": owner_oib or "",
        "owner_name": owner_name,
        "owner_addr": owner_addr,
    }


def split_sections(lines: list[Line]) -> list[list[Line]]:
    """One section per account: from a 'Valuta plaćanja' line to its 'NOVO STANJE'."""
    sections: list[list[Line]] = []
    cur: list[Line] | None = None
    for ln in lines:
        if "Valuta plaćanja" in ln.text:
            cur = [ln]
            sections.append(cur)
        elif cur is not None:
            cur.append(ln)
            if "NOVO STANJE" in ln.text:
                cur = None
    return sections


def is_entry_start(ln: Line) -> bool:
    row = ln.col(0, COL_ROW_MAX)
    if not (row and is_int_token(row[0].text)):
        return False
    ref1 = ln.col(COL_ROW_MAX, COL_REF1_MAX)
    return any(DATE_RE.search(w.text) for w in ref1)


def parse_section(section: list[Line], header: dict) -> Statement:
    head_line = section[0]
    m = re.search(r"Račun\s+([A-Z]{2}\d{2}[A-Z0-9]+)\s+Valuta plaćanja:\s*([A-Z]{3})", head_line.text)
    account_iban = m.group(1) if m else ""
    currency = m.group(2) if m else "EUR"

    # Opening balance: on the 'PRETHODNO STANJE' line (same as head_line here).
    opening = Decimal("0")
    opening_date = header["stmt_date"]
    for ln in section:
        if "PRETHODNO STANJE" in ln.text:
            amts = [w.text for w in sorted(ln.words, key=lambda w: w.x0)
                    if re.match(r"^-?\d[\d.]*,\d{2}$", w.text)]
            if amts:
                opening = parse_hr_amount(amts[-1])
            # prior statement date may be on the next line: "... 30.03.2026.)"
        # opening_date taken as the statement date (single-day statement)

    closing = Decimal("0")
    closing_date = header["stmt_date"]
    for ln in section:
        if "NOVO STANJE" in ln.text:
            amts = [w.text for w in sorted(ln.words, key=lambda w: w.x0)
                    if re.match(r"^-?\d[\d.]*,\d{2}$", w.text)]
            if amts:
                closing = parse_hr_amount(amts[-1])

    # Totals line (cross-check)
    tot_dbt_count = tot_crd_count = 0
    tot_dbt_sum = tot_crd_sum = Decimal("0")
    for ln in section:
        if "Ukupan broj" in ln.text:
            for w in ln.words:
                if w.text.isdigit():
                    if w.center < DEBIT_CREDIT_SPLIT_X and w.x0 > COL_REFDESC_MAX:
                        tot_dbt_count = int(w.text)
                    elif w.center >= DEBIT_CREDIT_SPLIT_X:
                        tot_crd_count = int(w.text)
        if "Ukupan iznos" in ln.text:
            for w in ln.words:
                if re.match(r"^-?\d[\d.]*,\d{2}$", w.text):
                    if w.center < DEBIT_CREDIT_SPLIT_X and w.x0 > COL_REFDESC_MAX:
                        tot_dbt_sum = parse_hr_amount(w.text)
                    elif w.center >= DEBIT_CREDIT_SPLIT_X:
                        tot_crd_sum = parse_hr_amount(w.text)

    # Entries
    entries: list[Entry] = []
    cur_lines: list[Line] = []

    def flush():
        if cur_lines:
            entries.append(_build_entry(cur_lines))

    started = False
    for ln in section:
        if "PRETHODNO STANJE" in ln.text:
            started = True
            continue
        if not started:
            continue
        if "Ukupan broj" in ln.text or "NOVO STANJE" in ln.text:
            break
        if is_entry_start(ln):
            flush()
            cur_lines = [ln]
        elif cur_lines:
            cur_lines.append(ln)
    flush()

    return Statement(
        account_iban=account_iban,
        currency=currency,
        stmt_no=header["stmt_no"],
        stmt_date=header["stmt_date"],
        owner_name=header["owner_name"],
        owner_addr=header["owner_addr"],
        owner_oib=header["owner_oib"],
        bank_name=header["bank_name"],
        bank_bic=header["bank_bic"],
        opening=opening,
        opening_date=opening_date,
        closing=closing,
        closing_date=closing_date,
        entries=entries,
        tot_dbt_count=tot_dbt_count,
        tot_crd_count=tot_crd_count,
        tot_dbt_sum=tot_dbt_sum,
        tot_crd_sum=tot_crd_sum,
    )


def _build_entry(lines: list[Line]) -> Entry:
    first = lines[0]
    booking = parse_hr_date(first.col_text(COL_ROW_MAX, COL_REF1_MAX))
    val_txt = first.col_text(COL_VALDATE_MAX - 67, COL_VALDATE_MAX)  # 145..212
    value = parse_hr_date(val_txt) if DATE_RE.search(val_txt) else booking

    # amount + direction from the first line
    amount = Decimal("0")
    cdt_dbt = "DBIT"
    for w in first.col(COL_REFDESC_MAX, 1000):
        if re.match(r"^-?\d[\d.]*,\d{2}$", w.text):
            amount = parse_hr_amount(w.text)
            cdt_dbt = "DBIT" if w.center < DEBIT_CREDIT_SPLIT_X else "CRDT"
            break

    # counterparty column (account + name/address), gathered over all lines
    cparty_iban = None
    cparty_parts: list[str] = []
    refs: list[str] = []
    poziv: list[str] = []
    desc_parts: list[str] = []

    for i, ln in enumerate(lines):
        cp = ln.col_text(COL_VALDATE_MAX, COL_CPARTY_MAX).strip()
        if cp:
            for tok in cp.split():
                if cparty_iban is None and IBAN_RE.match(tok):
                    cparty_iban = tok
                else:
                    cparty_parts.append(tok)
        # Referenca column (x60..145) — on continuation lines only (line 0 = booking date)
        if i > 0:
            r = ln.col_text(COL_ROW_MAX, COL_REF1_MAX).strip()
            if r and not DATE_RE.fullmatch(r.replace(" ", "")):
                refs.extend(r.split())
        # poziv + opis column (x318..456)
        rd = ln.col_text(COL_REFDESC_MAX - 138, COL_REFDESC_MAX).strip()  # 318..456
        if rd:
            if POZIV_RE.match(rd):
                poziv.append(rd)
            else:
                desc_parts.append(rd)

    poziv_platitelj = poziv[0] if len(poziv) >= 1 else ""
    poziv_primatelj = poziv[1] if len(poziv) >= 2 else ""

    return Entry(
        booking_date=booking,
        value_date=value,
        amount=amount,
        cdt_dbt=cdt_dbt,
        cparty_iban=cparty_iban,
        cparty_name=" ".join(cparty_parts).strip(),
        refs=refs,
        poziv_platitelj=poziv_platitelj,
        poziv_primatelj=poziv_primatelj,
        description=" ".join(desc_parts).strip(),
    )


# --------------------------------------------------------------------------- #
# camt.053.001.08 builder
# --------------------------------------------------------------------------- #
def _el(parent, tag, text=None, **attrib):
    e = etree.SubElement(parent, f"{{{CAMT_NS}}}{tag}", **attrib)
    if text is not None:
        e.text = str(text)
    return e


def _balance(stmt_el, code: str, amount: Decimal, ccy: str, date: str):
    bal = _el(stmt_el, "Bal")
    tp = _el(bal, "Tp")
    cdor = _el(tp, "CdOrPrtry")
    _el(cdor, "Cd", code)
    _el(bal, "Amt", amt_str(amount), Ccy=ccy)
    _el(bal, "CdtDbtInd", "DBIT" if amount < 0 else "CRDT")
    dt = _el(bal, "Dt")
    _el(dt, "Dt", date)


def build_camt(stmt: Statement, created: datetime) -> bytes:
    cre = created.strftime("%Y-%m-%dT%H:%M:%S")
    seq = re.sub(r"\D", "", stmt.stmt_no) or "0"
    yyyymmdd = stmt.stmt_date.replace("-", "")
    msg_id = f"BCS{stmt.owner_oib}{yyyymmdd}{int(seq):04d}"[:35]

    doc = etree.Element(f"{{{CAMT_NS}}}Document", nsmap={None: CAMT_NS})
    root = _el(doc, "BkToCstmrStmt")

    grp = _el(root, "GrpHdr")
    _el(grp, "MsgId", msg_id)
    _el(grp, "CreDtTm", cre)

    st = _el(root, "Stmt")
    _el(st, "Id", f"{stmt.stmt_no}/{yyyymmdd}")
    _el(st, "ElctrncSeqNb", int(seq))
    _el(st, "LglSeqNb", int(seq))
    _el(st, "CreDtTm", cre)
    frto = _el(st, "FrToDt")
    _el(frto, "FrDtTm", f"{stmt.stmt_date}T00:00:00")
    _el(frto, "ToDtTm", f"{stmt.stmt_date}T23:59:59")

    acct = _el(st, "Acct")
    acct_id = _el(acct, "Id")
    _el(acct_id, "IBAN", stmt.account_iban)
    _el(acct, "Ccy", stmt.currency)
    ownr = _el(acct, "Ownr")
    _el(ownr, "Nm", stmt.owner_name or "N/A")
    if stmt.owner_oib:
        oid = _el(ownr, "Id")
        org = _el(oid, "OrgId")
        othr = _el(org, "Othr")
        _el(othr, "Id", stmt.owner_oib)
        schme = _el(othr, "SchmeNm")
        _el(schme, "Prtry", "OIB")
    svcr = _el(acct, "Svcr")
    fin = _el(svcr, "FinInstnId")
    if stmt.bank_bic:
        _el(fin, "BICFI", stmt.bank_bic)
    _el(fin, "Nm", stmt.bank_name)

    _balance(st, "OPBD", stmt.opening, stmt.currency, stmt.opening_date)
    _balance(st, "CLBD", stmt.closing, stmt.currency, stmt.closing_date)

    dbt = [e for e in stmt.entries if e.cdt_dbt == "DBIT"]
    crd = [e for e in stmt.entries if e.cdt_dbt == "CRDT"]
    summ = _el(st, "TxsSummry")
    tot = _el(summ, "TtlNtries")
    _el(tot, "NbOfNtries", len(stmt.entries))
    tcd = _el(summ, "TtlCdtNtries")
    _el(tcd, "NbOfNtries", len(crd))
    _el(tcd, "Sum", amt_str(sum((e.amount for e in crd), Decimal("0"))))
    tdb = _el(summ, "TtlDbtNtries")
    _el(tdb, "NbOfNtries", len(dbt))
    _el(tdb, "Sum", amt_str(sum((e.amount for e in dbt), Decimal("0"))))

    for e in stmt.entries:
        _build_ntry(st, stmt, e)

    return etree.tostring(doc, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _build_ntry(stmt_el, stmt: Statement, e: Entry):
    ntry = _el(stmt_el, "Ntry")
    _el(ntry, "Amt", amt_str(e.amount), Ccy=stmt.currency)
    _el(ntry, "CdtDbtInd", e.cdt_dbt)
    _el(ntry, "RvslInd", "false")
    sts = _el(ntry, "Sts")
    _el(sts, "Cd", "BOOK")
    bd = _el(ntry, "BookgDt")
    _el(bd, "Dt", e.booking_date)
    vd = _el(ntry, "ValDt")
    _el(vd, "Dt", e.value_date)
    if e.refs:
        _el(ntry, "AcctSvcrRef", e.refs[0])
    # bank transaction code — generic best-effort (PMNT / misc op / not available)
    bk = _el(ntry, "BkTxCd")
    dom = _el(bk, "Domn")
    _el(dom, "Cd", "PMNT")
    fam = _el(dom, "Fmly")
    _el(fam, "Cd", "MDOP" if e.cdt_dbt == "DBIT" else "MCOP")
    _el(fam, "SubFmlyCd", "NTAV")

    ntdtls = _el(ntry, "NtryDtls")
    tx = _el(ntdtls, "TxDtls")

    refs = _el(tx, "Refs")
    if e.refs:  # schema order: AcctSvcrRef precedes EndToEndId
        _el(refs, "AcctSvcrRef", e.refs[0])
    _el(refs, "EndToEndId", poziv_ref(e.poziv_platitelj) or "NOTPROVIDED")
    for extra in e.refs[1:]:
        pr = _el(refs, "Prtry")
        _el(pr, "Tp", "REFERENCA")
        _el(pr, "Ref", extra)

    txamt = _el(tx, "Amt", amt_str(e.amount), Ccy=stmt.currency)  # noqa: F841
    _el(tx, "CdtDbtInd", e.cdt_dbt)

    if e.cparty_iban or e.cparty_name:
        rp = _el(tx, "RltdPties")
        # DBIT -> money leaves us: we are debtor, counterparty is creditor.
        # CRDT -> money comes in: we are creditor, counterparty is debtor.
        # Emit in schema order regardless of direction: Dbtr, DbtrAcct, Cdtr, CdtrAcct.
        if e.cdt_dbt == "DBIT":
            dbtr_nm, dbtr_iban = stmt.owner_name, None
            cdtr_nm, cdtr_iban = e.cparty_name, e.cparty_iban
        else:
            dbtr_nm, dbtr_iban = e.cparty_name, e.cparty_iban
            cdtr_nm, cdtr_iban = stmt.owner_name, None
        _el(_el(_el(rp, "Dbtr"), "Pty"), "Nm", (dbtr_nm or "N/A")[:140])
        if dbtr_iban:
            _el(_el(_el(rp, "DbtrAcct"), "Id"), "IBAN", dbtr_iban)
        _el(_el(_el(rp, "Cdtr"), "Pty"), "Nm", (cdtr_nm or "N/A")[:140])
        if cdtr_iban:
            _el(_el(_el(rp, "CdtrAcct"), "Id"), "IBAN", cdtr_iban)

    rmt_bits = []
    if e.description:
        rmt_bits.append(e.description)
    if rmt_bits or poziv_ref(e.poziv_primatelj):
        rmt = _el(tx, "RmtInf")
        for b in rmt_bits:
            _el(rmt, "Ustrd", b[:140])
        if poziv_ref(e.poziv_primatelj):
            strd = _el(rmt, "Strd")
            cri = _el(strd, "CdtrRefInf")
            tp = _el(cri, "Tp")
            cop = _el(tp, "CdOrPrtry")
            _el(cop, "Cd", "SCOR")
            _el(cri, "Ref", e.poziv_primatelj)

    if e.description:
        _el(ntry, "AddtlNtryInf", e.description[:500])


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def convert_pdf(pdf_path: str, out_dir: str, xsd: etree.XMLSchema | None, debug: bool) -> list[str]:
    lines = extract_lines(pdf_path)
    header = parse_header(lines)
    sections = split_sections(lines)
    if not sections:
        print(f"  [SKIP] {os.path.basename(pdf_path)}: no account section "
              f"('Valuta plaćanja') — not a ZABA PLPIZV2 statement")
        return []

    written: list[str] = []
    for sec in sections:
        stmt = parse_section(sec, header)
        if debug:
            _print_debug(pdf_path, stmt)

        # reconciliation check
        movement = sum((e.amount if e.cdt_dbt == "CRDT" else -e.amount) for e in stmt.entries)
        recon = stmt.opening + movement
        ok = recon == stmt.closing
        flag = "OK" if ok else "MISMATCH"
        print(f"  [{flag}] {os.path.basename(pdf_path)} acct {stmt.account_iban} "
              f"stmt {stmt.stmt_no}: open {stmt.opening} {'+' if movement>=0 else '-'} "
              f"{abs(movement)} = {recon} (PDF closing {stmt.closing}); "
              f"{len(stmt.entries)} entr(y/ies)")
        if not ok:
            print(f"      ! balance does not reconcile — check parsing", file=sys.stderr)
        # cross-check totals if present
        dbt_sum = sum((e.amount for e in stmt.entries if e.cdt_dbt == "DBIT"), Decimal("0"))
        crd_sum = sum((e.amount for e in stmt.entries if e.cdt_dbt == "CRDT"), Decimal("0"))
        if stmt.tot_dbt_sum and dbt_sum != stmt.tot_dbt_sum:
            print(f"      ! debit total {dbt_sum} != PDF 'Ukupan' {stmt.tot_dbt_sum}", file=sys.stderr)
        if stmt.tot_crd_sum and crd_sum != stmt.tot_crd_sum:
            print(f"      ! credit total {crd_sum} != PDF 'Ukupan' {stmt.tot_crd_sum}", file=sys.stderr)

        xml = build_camt(stmt, datetime.now())

        if xsd is not None:
            root = etree.fromstring(xml)
            if not xsd.validate(root):
                print(f"      ! XSD validation failed: {xsd.error_log}", file=sys.stderr)
            else:
                print(f"      [XSD OK]")

        seq = re.sub(r"\D", "", stmt.stmt_no) or "0"
        fname = f"BCS.{stmt.owner_oib or 'NA'}.{stmt.stmt_date.replace('-', '')}.{int(seq):04d}.xml"
        out_path = os.path.join(out_dir, fname)
        with open(out_path, "wb") as fh:
            fh.write(xml)
        written.append(out_path)
        print(f"      -> {out_path}")
    return written


def _print_debug(pdf_path: str, stmt: Statement):
    print(f"\n--- DEBUG {os.path.basename(pdf_path)} ---")
    for k in ("account_iban", "currency", "stmt_no", "stmt_date", "owner_name",
              "owner_oib", "bank_name", "bank_bic", "opening", "closing"):
        print(f"  {k}: {getattr(stmt, k)}")
    for i, e in enumerate(stmt.entries, 1):
        print(f"  entry {i}: {e.cdt_dbt} {e.amount} book={e.booking_date} val={e.value_date}")
        print(f"      cparty: {e.cparty_iban} | {e.cparty_name}")
        print(f"      poziv P/R: {e.poziv_platitelj} / {e.poziv_primatelj}")
        print(f"      refs: {e.refs}")
        print(f"      opis: {e.description}")
    print("--- /DEBUG ---\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="ZABA PDF statement -> camt.053.001.08 XML")
    ap.add_argument("--in", dest="indir", default=".", help="folder with PDF statements")
    ap.add_argument("--out", dest="outdir", default="./out", help="output folder for XML")
    ap.add_argument("--xsd", dest="xsd", default=None, help="optional camt.053.001.08 XSD to validate against")
    ap.add_argument("--debug", action="store_true", help="print parsed structure")
    args = ap.parse_args(argv)

    os.makedirs(args.outdir, exist_ok=True)
    xsd = etree.XMLSchema(etree.parse(args.xsd)) if args.xsd else None

    # Recurse into subfolders (statements are often organised in dated folders).
    pdfs = sorted(set(
        glob.glob(os.path.join(args.indir, "**", "*.pdf"), recursive=True) +
        glob.glob(os.path.join(args.indir, "**", "*.PDF"), recursive=True)
    ))
    if not pdfs:
        print(f"No PDFs found in {args.indir}", file=sys.stderr)
        return 1

    print(f"Converting {len(pdfs)} PDF(s) from {args.indir} -> {args.outdir}")
    total = 0
    for p in pdfs:
        try:
            written = convert_pdf(p, args.outdir, xsd, args.debug)
            total += len(written)
        except Exception as exc:  # noqa: BLE001 — POC: report and continue
            print(f"  [ERROR] {os.path.basename(p)}: {exc}", file=sys.stderr)
    print(f"Done. Wrote {total} XML file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
