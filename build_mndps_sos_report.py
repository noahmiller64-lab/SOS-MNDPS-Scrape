#!/usr/bin/env python3
"""Build and validate the final plain-text and PDF SOS deliverables."""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

import scrape_mndps_sos as scraper


def escaped(value: Any) -> str:
    return html_lib.escape(scraper.clean(str(value)), quote=True)


def load_complete_checkpoint(path: Path, names: list[str]) -> list[dict[str, Any]]:
    records = scraper.load_checkpoint(path, names)
    if len(records) != len(names):
        raise scraper.ScrapeError(
            f"Cannot build final report: checkpoint has {len(records):,} of {len(names):,} names"
        )
    for index, record in enumerate(records, 1):
        if record.get("index") != index or record.get("name") != names[index - 1]:
            raise scraper.ScrapeError(f"Checkpoint continuity failure at record {index}")
        attempts = record.get("attempts", [])
        if not attempts:
            raise scraper.ScrapeError(f"Record {index} has no recorded search attempts")
        for attempt in attempts:
            query = attempt.get("url", "")
            parsed = urllib_parse(query)
            if parsed.get("Type") != ["BeginsWith"] or parsed.get("IncludePriorNames") != ["True"]:
                raise scraper.ScrapeError(f"Record {index} violates the required search settings")
        if record.get("matched"):
            count = int(record.get("result_count", 0))
            matches = record.get("matches", [])
            if count < 1 or len(matches) != min(3, count):
                raise scraper.ScrapeError(f"Record {index} has an invalid match/detail count")
            for match in matches:
                if not match.get("url") or not match.get("status_line"):
                    raise scraper.ScrapeError(f"Record {index} has an incomplete opened match")
                if not match.get("fields") and not match.get("detail_error"):
                    raise scraper.ScrapeError(f"Record {index} has an incomplete opened match")
        elif record.get("result_count") != 0 or record.get("matches"):
            raise scraper.ScrapeError(f"Record {index} has inconsistent no-result data")
    return records


def urllib_parse(url: str) -> dict[str, list[str]]:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)


def header_lines(records: list[dict[str, Any]]) -> list[str]:
    matched = sum(bool(record.get("matched")) for record in records)
    no_results = len(records) - matched
    return [
        f"Source list: {scraper.SOURCE_DESCRIPTION}",
        (
            "Search rules: Scope = Begins With; Include Prior Names = Include; Filing Status = "
            "Active first, Inactive if no active results; suffix-drop fallback; first up to 3 "
            "matches opened per name."
        ),
        f"Searched {len(records):,} names: {matched:,} with matches, "
        f"{no_results:,} with no results (Active or Inactive).",
        f"Portal: {scraper.PORTAL_URL}  Generated: {scraper.dt.date.today().isoformat()}",
    ]


def make_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=19,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#17365D"),
            spaceAfter=10,
        ),
        "header": ParagraphStyle(
            "HeaderBlock",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8.4,
            leading=11,
            leftIndent=8,
            rightIndent=8,
            borderColor=colors.HexColor("#B8C6D1"),
            borderWidth=0.6,
            borderPadding=7,
            backColor=colors.HexColor("#F3F6F8"),
            textColor=colors.HexColor("#243447"),
            spaceAfter=12,
        ),
        "name": ParagraphStyle(
            "NameHeader",
            parent=base["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=9.2,
            leading=11.5,
            textColor=colors.HexColor("#17365D"),
            backColor=colors.HexColor("#EAF0F5"),
            borderColor=colors.HexColor("#B8C6D1"),
            borderWidth=0.35,
            borderPadding=4,
            spaceBefore=8,
            spaceAfter=3,
            keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "RecordBody",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=7.8,
            leading=10,
            leftIndent=12,
            alignment=TA_LEFT,
            spaceAfter=2,
        ),
        "match": ParagraphStyle(
            "MatchHeader",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8.2,
            leading=10.5,
            leftIndent=18,
            textColor=colors.HexColor("#222222"),
            spaceBefore=4,
            spaceAfter=2,
            keepWithNext=True,
        ),
        "detail": ParagraphStyle(
            "DetailLine",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=7.2,
            leading=9,
            leftIndent=30,
            firstLineIndent=0,
            spaceAfter=1,
        ),
        "history_label": ParagraphStyle(
            "HistoryLabel",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=7.2,
            leading=9,
            leftIndent=30,
            spaceBefore=2,
            spaceAfter=1,
            keepWithNext=True,
        ),
        "history": ParagraphStyle(
            "HistoryLine",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=6.8,
            leading=8.5,
            leftIndent=42,
            firstLineIndent=-7,
            bulletIndent=34,
            spaceAfter=0.8,
        ),
    }


def paragraph(text: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escaped(text), style)


def record_story(record: dict[str, Any], styles: dict[str, ParagraphStyle]) -> Iterable[Any]:
    yield paragraph(f"{record['index']}. {record['name'].upper()}", styles["name"])
    if not record.get("matched"):
        if record.get("suffix_drop_applicable"):
            message = "No results (Begins With, Active and Inactive, suffix-drop attempted)."
        else:
            message = "No results (Begins With, Active and Inactive; suffix-drop not applicable)."
        yield paragraph(message, styles["body"])
        return

    yield paragraph(
        f"Filing Status used: {record['filing_status_used']}; "
        f"{record['result_count']} match(es) returned",
        styles["body"],
    )
    if record.get("suffix_dropped"):
        yield paragraph(
            f"Suffix-drop search string used: {record['search_name_used']}", styles["body"]
        )
    for match_number, match in enumerate(record.get("matches", []), 1):
        yield paragraph(f"Match {match_number}: {match['business_name']}", styles["match"])
        yield paragraph(match["status_line"], styles["detail"])
        if match.get("prior_name_hit"):
            yield paragraph("Prior-name hit: Yes", styles["detail"])
        url = escaped(match["url"])
        yield Paragraph(f'<link href="{url}" color="#1A5A96">{url}</link>', styles["detail"])
        if match.get("detail_error"):
            yield paragraph(
                f"Detail page unavailable after 4 attempts: {match['detail_error']}",
                styles["detail"],
            )
            continue
        for field in match.get("fields", []):
            values = "; ".join(scraper.clean(str(value)) for value in field.get("values", []))
            yield Paragraph(
                f"<b>{escaped(field.get('label', ''))}:</b> {escaped(values)}", styles["detail"]
            )
        yield paragraph("Filing History:", styles["history_label"])
        filing_history = match.get("filing_history", [])
        if filing_history:
            for entry in filing_history:
                yield Paragraph(f"- {escaped(entry)}", styles["history"])
        else:
            yield Paragraph("- None shown", styles["history"])
        yield paragraph("Renewal History:", styles["history_label"])
        renewal_history = match.get("renewal_history", [])
        if renewal_history:
            for entry in renewal_history:
                yield Paragraph(f"- {escaped(entry)}", styles["history"])
        else:
            yield Paragraph("- None shown", styles["history"])


def footer(canvas: Any, doc: Any) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D5DCE2"))
    canvas.setLineWidth(0.35)
    canvas.line(doc.leftMargin, 0.43 * inch, letter[0] - doc.rightMargin, 0.43 * inch)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.HexColor("#5F6B75"))
    canvas.drawRightString(letter[0] - doc.rightMargin, 0.25 * inch, f"Page {doc.page}")
    canvas.restoreState()


def build_pdf(path: Path, records: list[dict[str, Any]]) -> None:
    styles = make_styles()
    story: list[Any] = [
        paragraph("Minnesota Secretary of State - Business Search Results", styles["title"]),
        Paragraph("<br/>".join(escaped(line) for line in header_lines(records)), styles["header"]),
    ]
    for record in records:
        story.extend(record_story(record, styles))
    path.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.55 * inch,
        title="Minnesota Secretary of State - Business Search Results",
        author="Noah Miller",
        subject=(
            "Minnesota Secretary of State business search results for "
            f"{scraper.EXPECTED_NAME_COUNT:,} MNDPS payee names"
        ),
        pageCompression=1,
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)


def validate_pdf(path: Path, expected_last_name: str) -> tuple[int, int]:
    if not path.exists() or path.stat().st_size == 0:
        raise scraper.ScrapeError(f"PDF was not created: {path}")
    reader = PdfReader(str(path))
    page_count = len(reader.pages)
    if page_count < 2:
        raise scraper.ScrapeError(f"Unexpected PDF page count: {page_count}")
    first_text = reader.pages[0].extract_text() or ""
    trailing_text = "\n".join(
        (page.extract_text() or "") for page in reader.pages[max(0, page_count - 5) :]
    )
    if "Minnesota Secretary of State - Business Search Results" not in first_text:
        raise scraper.ScrapeError("PDF title missing from first page")
    if expected_last_name.upper() not in trailing_text.upper():
        raise scraper.ScrapeError("Final input name missing from the final five PDF pages")
    return page_count, path.stat().st_size


def git_final(repo: Path, files: list[Path], push: bool) -> None:
    subprocess.run(
        ["git", "add", "--", *(str(path.relative_to(repo)) for path in files)],
        cwd=repo,
        check=True,
    )
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo)
    if staged.returncode != 0:
        subprocess.run(
            ["git", "commit", "-m", "Add final Minnesota SOS search deliverables"],
            cwd=repo,
            check=True,
        )
    if push:
        subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=repo, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("sos_names_mndps_all_fy_tabs.txt"))
    parser.add_argument(
        "--checkpoint", type=Path, default=Path("MNDPS_SOS_Search_Checkpoint.jsonl")
    )
    parser.add_argument(
        "--text-output", type=Path, default=Path("MNDPS_SOS_Search_Results_AllTabs.txt")
    )
    parser.add_argument(
        "--pdf-output", type=Path, default=Path("MNDPS_SOS_Search_Results_AllTabs.pdf")
    )
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()

    repo = Path.cwd().resolve()
    resolve = lambda value: value.resolve() if value.is_absolute() else (repo / value).resolve()
    names = scraper.load_names(resolve(args.input))
    records = load_complete_checkpoint(resolve(args.checkpoint), names)
    text_output = resolve(args.text_output)
    pdf_output = resolve(args.pdf_output)

    scraper.write_text_report(text_output, records, pilot=False)
    build_pdf(pdf_output, records)
    page_count, size = validate_pdf(pdf_output, names[-1])
    final_label = f"{len(names)}. {names[-1].upper()}"
    if final_label not in text_output.read_text(encoding="utf-8"):
        raise scraper.ScrapeError("Final text output is missing the final numbered name")
    print(
        json.dumps(
            {
                "records": len(records),
                "matched": sum(bool(record.get("matched")) for record in records),
                "no_results": sum(not record.get("matched") for record in records),
                "pdf_pages": page_count,
                "pdf_bytes": size,
                "text_bytes": text_output.stat().st_size,
            },
            indent=2,
        ),
        flush=True,
    )
    git_final(repo, [resolve(args.checkpoint), text_output, pdf_output], args.push)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except scraper.ScrapeError as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr, flush=True)
        raise SystemExit(2)
