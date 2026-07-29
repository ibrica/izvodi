#!/usr/bin/env python3
"""
Unwrap Privredna banka Zagreb (PBZ) signed statement files (*.zip.sgn) and emit
camt.053.001.08 XML for ERP import.

A PBZ .sgn file is NOT a zip despite the name. It is an XML envelope:

    <FileUpload>
      <TimeStamp>...</TimeStamp>
      <File compress="zip" encoding="base64">...</File>   <- base64 of a ZIP
      <Signer/><Certificate>..hex..</Certificate><Sign>..hex..</Sign>
      <Time>...</Time>
    </FileUpload>

The ZIP (sometimes nested one level) contains a camt.053 XML — but PBZ issues it as
**camt.053.001.02** (ISO 2009), while ERPs following the current Croatian HUB/FINA
standard expect **camt.053.001.08**. This script unwraps and upconverts.

.02 -> .08 changes applied (each verified against the official camt.053.001.08 XSD;
the rename set was derived by diffing the .02 and .08 schemas, not by guesswork):
  1. namespace            urn:...camt.053.001.02  ->  urn:...camt.053.001.08
  2. entry status         <Sts>BOOK</Sts>         ->  <Sts><Cd>BOOK</Cd></Sts>
                          (EntryStatus1Choice)
  3. related parties      <Dbtr><Nm>..            ->  <Dbtr><Pty><Nm>..
                          (Party40Choice: InitgPty/Dbtr/UltmtDbtr/Cdtr/UltmtCdtr/TradgPty)
  4. financial institution <FinInstnId><BIC>      ->  <FinInstnId><BICFI>
  5. charges              <Chrgs><Amt>..          ->  <Chrgs><Rcrd><Amt>..
                          (ChargesInformation6 -> Charges6/ChargesRecord3; note .02
                          allows repeating <Chrgs> while .08 allows one with repeating
                          <Rcrd>, so multiples are merged. <Pty> becomes <Agt>.)
  6. organisation id      <OrgId><BICOrBEI>       ->  <OrgId><AnyBIC>
  7. summary net total    <TtlNetNtryAmt Ccy=..>  ->  <TtlNetNtry><Amt>..</Amt>..
                          (Ccy attribute dropped — .08 types it as a plain decimal)
  8. remittance location  flat <RmtLctnMtd>..     ->  <RmtLctnDtls><Mtd>..
  9. tax admin zone       <AdmstnZn>              ->  <AdmstnZone>

NOTE: the envelope's digital signature is NOT verified — the payload is only
extracted. Treat provenance as you would the original file from the bank.

Usage:
    python3 pbz_sgn_to_camt053.py --in ./in --out ./out --xsd camt.053.001.08.xsd
"""
from __future__ import annotations

import argparse
import base64
import glob
import io
import os
import re
import sys
import zipfile
from decimal import Decimal

from lxml import etree

# Windows consoles and redirected output default to a legacy code page (cp1252),
# which cannot encode Croatian characters (č ć ž š đ) in account/party names.
# Force UTF-8 so `python ... > log.txt` never dies with UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # non-reconfigurable stream
        pass

NS02 = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"
NS08 = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"

# Elements typed Party40Choice in .08 -> their content must sit inside <Pty>.
PARTY_CHOICE = {"InitgPty", "Dbtr", "UltmtDbtr", "Cdtr", "UltmtCdtr", "TradgPty"}

# Plain renames .02 -> .08. These names are unambiguous schema-wide; <BIC> is
# handled separately because it needs a parent guard.
RENAMES = {"BICOrBEI": "AnyBIC", "AdmstnZn": "AdmstnZone"}

# RemittanceLocation2 (.02, flat) -> RemittanceLocation7 (.08, nested in RmtLctnDtls).
RMT_LCTN = {"RmtLctnMtd": "Mtd",
            "RmtLctnElctrncAdr": "ElctrncAdr",
            "RmtLctnPstlAdr": "PstlAdr"}


def _q(name: str) -> str:
    return f"{{{NS08}}}{name}"


def _renamed(local: str, parent_local: str | None) -> str:
    if local == "BIC" and parent_local == "FinInstnId":
        return "BICFI"
    return RENAMES.get(local, local)

MAX_ZIP_DEPTH = 5


# --------------------------------------------------------------------------- #
# Unwrapping
# --------------------------------------------------------------------------- #
def unwrap_sgn(path: str) -> bytes:
    """PBZ .sgn envelope -> raw bytes of the embedded (base64+zip) payload."""
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False,
                             no_network=True, recover=True)
    root = etree.parse(path, parser).getroot()
    node = root.find(".//File")
    if node is None or not (node.text or "").strip():
        raise ValueError("no <File> payload found — not a PBZ .sgn envelope?")
    if (node.get("encoding") or "base64").lower() != "base64":
        raise ValueError(f"unexpected encoding={node.get('encoding')!r}")
    return base64.b64decode(node.text)


def extract_xml(blob: bytes, depth: int = 0) -> bytes:
    """Peel (possibly nested) ZIP layers until the camt XML falls out."""
    if depth > MAX_ZIP_DEPTH:
        raise ValueError("too many nested zip layers")
    if blob[:2] != b"PK":
        return blob  # already the XML
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if not names:
            raise ValueError("empty zip")
        # prefer an .xml member, else recurse into the single member
        xmls = [n for n in names if n.lower().endswith(".xml")]
        pick = xmls[0] if xmls else names[0]
        return extract_xml(zf.read(pick), depth + 1)


def payload_of(path: str) -> bytes:
    """Accept .sgn / .zip / .xml transparently."""
    with open(path, "rb") as fh:
        head = fh.read(4)
    if head[:2] == b"PK":
        return extract_xml(open(path, "rb").read())
    if head[:1] == b"<":
        # either the .sgn envelope or a bare camt XML
        raw = open(path, "rb").read()
        if b"<FileUpload" in raw[:512]:
            return extract_xml(unwrap_sgn(path))
        return raw
    raise ValueError("unrecognised file (not zip, not XML)")


# --------------------------------------------------------------------------- #
# .02 -> .08 upconversion
# --------------------------------------------------------------------------- #
def _convert(el: etree._Element, parent_local: str | None = None,
             nsmap: dict | None = None) -> etree._Element:
    local = etree.QName(el).localname
    # nsmap is passed only for the root, so the whole tree serialises with an
    # unprefixed default namespace (matching what the banks themselves emit —
    # prefixed output is schema-valid but trips naive ERP parsers).
    new = etree.Element(_q(_renamed(local, parent_local)), nsmap=nsmap)
    for k, v in el.attrib.items():
        new.set(k, v)

    # (2) <Sts>BOOK</Sts> -> <Sts><Cd>BOOK</Cd></Sts>
    if local == "Sts" and len(el) == 0 and (el.text or "").strip():
        cd = etree.SubElement(new, _q("Cd"))
        cd.text = el.text.strip()
        return new

    # (5) ChargesInformation6 (flat) -> Charges6 { TtlChrgsAndTaxAmt?, Rcrd* }.
    # Skip if already .08-shaped (has Rcrd) so passthrough stays a no-op.
    if (local == "Chrgs" and len(el)
            and not any(etree.QName(c).localname == "Rcrd" for c in el)):
        rcrd_kids = []
        for c in el:
            if etree.QName(c).localname == "TtlChrgsAndTaxAmt":
                new.append(_convert(c, "Chrgs"))
            else:
                rcrd_kids.append(c)
        if rcrd_kids:
            rcrd = etree.SubElement(new, _q("Rcrd"))
            for c in rcrd_kids:
                conv = _convert(c, "Rcrd")
                # ChargesInformation6/Pty -> ChargesRecord3/Agt
                if etree.QName(c).localname == "Pty":
                    conv.tag = _q("Agt")
                rcrd.append(conv)
        return new

    # (3) Party40Choice: wrap children in <Pty> (only inside RltdPties).
    # Skip if already a choice (Pty/Agt) so re-running on .08 input is a no-op.
    if (local in PARTY_CHOICE and parent_local == "RltdPties" and len(el)
            and etree.QName(el[0]).localname not in ("Pty", "Agt")):
        pty = etree.SubElement(new, f"{{{NS08}}}Pty")
        for child in el:
            pty.append(_convert(child, "Pty"))
        return new

    # Drop whitespace-only text so pretty_print can re-indent cleanly.
    new.text = el.text if (el.text and el.text.strip()) else None
    for child in el:
        new.append(_convert(child, local))
    _restructure(new, local)
    return new


def _restructure(new: etree._Element, local: str) -> None:
    """Fixes that need sibling context, applied after children are converted."""
    if local in ("Ntry", "TxDtls"):
        _merge_charges(new)
    elif local == "TtlNtries":
        _fix_total_net(new)
    elif local == "RltdRmtInf":
        _fix_rmt_lctn(new)


def _merge_charges(parent: etree._Element) -> None:
    """.02 allows repeating <Chrgs>; .08 allows one <Chrgs> with repeating <Rcrd>."""
    chrgs = [c for c in parent if c.tag == _q("Chrgs")]
    if len(chrgs) < 2:
        return
    first, rest = chrgs[0], chrgs[1:]
    for extra in rest:
        for rcrd in [r for r in extra if r.tag == _q("Rcrd")]:
            first.append(rcrd)
        # keep a total only if the first one lacks it
        if first.find(_q("TtlChrgsAndTaxAmt")) is None:
            tot = extra.find(_q("TtlChrgsAndTaxAmt"))
            if tot is not None:
                first.insert(0, tot)
        parent.remove(extra)


def _fix_total_net(parent: etree._Element) -> None:
    """<TtlNetNtryAmt Ccy=..>X</><CdtDbtInd>Y</> -> <TtlNetNtry><Amt>X</><CdtDbtInd>Y</></>.

    Note .08 types the amount as NonNegativeDecimalNumber, so the Ccy attribute
    present in .02 must be dropped.
    """
    amt = parent.find(_q("TtlNetNtryAmt"))
    if amt is None:
        return
    idx = list(parent).index(amt)
    net = etree.Element(_q("TtlNetNtry"))
    a = etree.SubElement(net, _q("Amt"))
    a.text = amt.text
    cdi = parent.find(_q("CdtDbtInd"))
    if cdi is not None:
        c = etree.SubElement(net, _q("CdtDbtInd"))
        c.text = cdi.text
        parent.remove(cdi)
    parent.remove(amt)
    parent.insert(idx, net)


def _fix_rmt_lctn(parent: etree._Element) -> None:
    """Flat RmtLctn* (.02) -> nested <RmtLctnDtls> (.08)."""
    found = [(c, RMT_LCTN[etree.QName(c).localname]) for c in parent
             if etree.QName(c).localname in RMT_LCTN]
    if not found:
        return
    idx = list(parent).index(found[0][0])
    dtls = etree.Element(_q("RmtLctnDtls"))
    for c, newname in found:
        parent.remove(c)
        c.tag = _q(newname)
        dtls.append(c)
    parent.insert(idx, dtls)


def upconvert(xml_bytes: bytes) -> tuple[bytes, str]:
    """Return (camt.053.001.08 bytes, source_version_label)."""
    root = etree.fromstring(xml_bytes)
    ns = etree.QName(root).namespace
    if ns not in (NS02, NS08):
        raise ValueError(f"unsupported namespace: {ns}")
    label = "02" if ns == NS02 else "08 (passthrough)"
    # .08 input goes through the same transform — every rule is a no-op there,
    # but it normalises namespace prefixes and indentation.
    new_root = _convert(root, nsmap={None: NS08})
    etree.cleanup_namespaces(new_root)
    return etree.tostring(new_root.getroottree(), xml_declaration=True,
                          encoding="UTF-8", pretty_print=True), label


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def summarise(xml: bytes) -> str:
    """Reconcile OPBD/CLBD against entries; return a one-line report."""
    r = etree.fromstring(xml)
    ns = {"c": NS08}
    out = []
    for stmt in r.findall(".//c:Stmt", ns):
        iban = stmt.findtext("c:Acct/c:Id/c:IBAN", "", ns)
        bals = {}
        for b in stmt.findall("c:Bal", ns):
            code = b.findtext("c:Tp/c:CdOrPrtry/c:Cd", "", ns)
            amt = Decimal(b.findtext("c:Amt", "0", ns))
            if b.findtext("c:CdtDbtInd", "CRDT", ns) == "DBIT":
                amt = -amt
            bals[code] = amt
        move = Decimal("0")
        n = 0
        for e in stmt.findall("c:Ntry", ns):
            a = Decimal(e.findtext("c:Amt", "0", ns))
            move += a if e.findtext("c:CdtDbtInd", "CRDT", ns) == "CRDT" else -a
            n += 1
        op, cl = bals.get("OPBD"), bals.get("CLBD")
        if op is not None and cl is not None:
            ok = (op + move) == cl
            out.append(f"{iban}: {op} {'+' if move >= 0 else '-'} {abs(move)} = {op + move} "
                       f"(closing {cl}) [{'OK' if ok else 'MISMATCH'}] {n} entr(y/ies)")
        else:
            out.append(f"{iban}: {n} entr(y/ies) (no OPBD/CLBD pair)")
    return "; ".join(out)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="PBZ .sgn -> camt.053.001.08")
    ap.add_argument("--in", dest="indir", default=".", help="folder to scan (recursive)")
    ap.add_argument("--out", dest="outdir", default="./out", help="output folder")
    ap.add_argument("--xsd", dest="xsd", default=None, help="camt.053.001.08 XSD to validate against")
    args = ap.parse_args(argv)

    os.makedirs(args.outdir, exist_ok=True)
    xsd = etree.XMLSchema(etree.parse(args.xsd)) if args.xsd else None

    files = sorted(set(
        glob.glob(os.path.join(args.indir, "**", "*.sgn"), recursive=True) +
        glob.glob(os.path.join(args.indir, "**", "*.SGN"), recursive=True)
    ))
    if not files:
        print(f"No .sgn files found in {args.indir}", file=sys.stderr)
        return 1

    print(f"Converting {len(files)} file(s) from {args.indir} -> {args.outdir}")
    written = 0
    for p in files:
        name = os.path.basename(p)
        try:
            src_xml = payload_of(p)
            out_xml, srcver = upconvert(src_xml)
            print(f"  [camt.053.001.{srcver}] {name}")
            print(f"      {summarise(out_xml)}")

            if xsd is not None:
                if xsd.validate(etree.fromstring(out_xml)):
                    print("      [XSD OK]")
                else:
                    print(f"      ! XSD validation FAILED: {xsd.error_log}", file=sys.stderr)

            base = re.sub(r"\.zip\.sgn$|\.sgn$", "", name, flags=re.I)
            out_path = os.path.join(args.outdir, f"{base}.camt053_08.xml")
            with open(out_path, "wb") as fh:
                fh.write(out_xml)
            print(f"      -> {out_path}")
            written += 1
        except Exception as exc:  # noqa: BLE001 — report and continue
            print(f"  [ERROR] {name}: {exc}", file=sys.stderr)
    print(f"Done. Wrote {written} XML file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
