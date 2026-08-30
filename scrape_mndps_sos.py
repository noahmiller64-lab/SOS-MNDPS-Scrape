#!/usr/bin/env python3
"""Resumable, rate-limited Minnesota SOS business-search scraper.

The checkpoint is append-only JSONL. On every launch the next input name is
derived from the validated checkpoint, so an interrupted run never restarts at
name 1. Checkpoint commits can also be pushed periodically.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from lxml import html


BASE_URL = "https://mblsportal.sos.mn.gov"
SEARCH_URL = f"{BASE_URL}/Business/BusinessSearch"
PORTAL_URL = f"{BASE_URL}/Business/Search"
EXPECTED_NAME_COUNT = 3_308
SOURCE_DESCRIPTION = (
    "MNDPS all-FY-tabs payee list (FY20-FY26), exact-text de-duplicated, "
    f"{EXPECTED_NAME_COUNT:,} names."
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36 SOS-Scrape/1.0"
)
SUFFIX_RE = re.compile(r"(?i)^(?P<stem>.*\S)\s*,?\s+(?P<suffix>[A-Z.]+),?$")
CORPORATE_SUFFIXES = {"INC", "LLC", "CORP", "CO", "LTD", "LLP", "LP", "PA", "PLLC"}
DASHES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-"})


class ScrapeError(RuntimeError):
    pass


def clean(value: str) -> str:
    value = value.translate(DASHES).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def node_parts(node: Any) -> list[str]:
    return [piece for piece in (clean(t) for t in node.xpath(".//text()")) if piece]


def node_text(node: Any, separator: str = " | ") -> str:
    return separator.join(node_parts(node))


class PoliteClient:
    def __init__(self, minimum_delay: float = 1.05, timeout: float = 60.0) -> None:
        self.minimum_delay = max(1.0, minimum_delay)
        self.current_delay = self.minimum_delay
        self.timeout = timeout
        self.last_request_started = 0.0

    def _wait(self) -> None:
        remaining = self.current_delay - (time.monotonic() - self.last_request_started)
        if remaining > 0:
            time.sleep(remaining)

    def get(self, url: str) -> bytes:
        last_error: Exception | None = None
        # Initial attempt plus up to three retries, as required by the spec.
        for attempt in range(4):
            self._wait()
            self.last_request_started = time.monotonic()
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Cache-Control": "no-cache",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = response.read()
                    status = getattr(response, "status", 200)
                if status != 200:
                    raise ScrapeError(f"HTTP {status} for {url}")
                if len(data) < 1000:
                    raise ScrapeError(f"Unexpectedly short response ({len(data)} bytes) for {url}")
                self.current_delay = max(self.minimum_delay, self.current_delay * 0.85)
                return data
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ScrapeError) as exc:
                last_error = exc
                refusing = isinstance(exc, urllib.error.HTTPError) and exc.code in {403, 429, 500, 502, 503, 504}
                if refusing:
                    self.current_delay = min(30.0, max(5.0, self.current_delay * 2.0))
                if attempt == 3:
                    break
                backoff = min(60.0, (2**attempt) * (5.0 if refusing else 2.0))
                print(
                    f"Request failed ({attempt + 1}/4): {exc}; waiting {backoff:.0f}s before retry",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(backoff)
        raise ScrapeError(f"Request failed after 4 attempts: {url}: {last_error}")


def search_url(name: str, status: str) -> str:
    query = urllib.parse.urlencode(
        {
            "BusinessName": name,
            "IncludePriorNames": "True",
            "Status": status,
            "Type": "BeginsWith",
        }
    )
    return f"{SEARCH_URL}?{query}"


def parse_search_page(data: bytes, requested_status: str) -> list[dict[str, Any]]:
    try:
        doc = html.fromstring(data)
    except Exception as exc:
        raise ScrapeError(f"Could not parse search response: {exc}") from exc

    page_text = clean(doc.text_content())
    title = clean(" ".join(doc.xpath("//title/text()")))
    if "Business Filing Search" not in title or "Search Results" not in page_text:
        raise ScrapeError(f"Unexpected search response page: title={title!r}")

    links = doc.xpath(
        '//table[contains(concat(" ", normalize-space(@class), " "), " selectable ")]'
        '//a[contains(@href, "/Business/SearchDetails?filingGuid=")]'
    )
    if not links:
        if "No results match the criteria entered." not in page_text:
            raise ScrapeError("Search page had neither detail links nor the expected no-results message")
        return []

    results: list[dict[str, Any]] = []
    for link in links:
        row = link.xpath("ancestor::tr[1]")
        if not row:
            raise ScrapeError("Detail link was not inside a result row")
        row = row[0]
        strong = row.xpath(".//strong[1]")
        business_name = node_text(strong[0], " ") if strong else ""
        values = [node_text(span, " ") for span in row.xpath(".//small//span")]
        values = [value for value in values if value]
        if not business_name or len(values) < 3:
            raise ScrapeError(f"Could not parse result row: {clean(row.text_content())[:300]}")
        status, business_type, name_type = values[:3]
        href = link.get("href")
        if not href:
            raise ScrapeError("Result detail link had no href")
        results.append(
            {
                "business_name": business_name,
                "business_status": status,
                "business_type": business_type,
                "name_type": name_type,
                "status_line": f"{status} | {business_type} | {name_type}",
                "prior_name_hit": "prior" in name_type.lower(),
                "url": urllib.parse.urljoin(BASE_URL, href),
                "requested_status": requested_status,
            }
        )
    return results


def parse_history_rows(rows: Iterable[Any], renewal: bool = False) -> list[str]:
    history: list[str] = []
    for row in rows:
        date_nodes = row.xpath('./td[contains(concat(" ", normalize-space(@class), " "), " date ")]')
        action_nodes = row.xpath('./td[contains(concat(" ", normalize-space(@class), " "), " action ")]')
        if not date_nodes or not action_nodes:
            continue
        filing_date = node_text(date_nodes[0], " ")
        action = node_text(action_nodes[0], " ")
        if not filing_date and not action:
            continue
        entry = " - ".join(part for part in (filing_date, action) if part)
        if not renewal and len(date_nodes) > 1:
            effective_date = node_text(date_nodes[1], " ")
            if effective_date:
                entry += f" - Effective Date: {effective_date}"
        history.append(entry)
    return history


def parse_detail_page(data: bytes, url: str) -> dict[str, Any]:
    try:
        doc = html.fromstring(data)
    except Exception as exc:
        raise ScrapeError(f"Could not parse detail response {url}: {exc}") from exc

    title = clean(" ".join(doc.xpath("//title/text()")))
    summary = doc.xpath('//*[@id="filingSummary"]')
    if "Business Filing Details" not in title or not summary:
        raise ScrapeError(f"Unexpected detail response for {url}: title={title!r}")

    fields: list[dict[str, Any]] = []
    for dl in summary[0].xpath(".//dl"):
        labels = dl.xpath("./dt")
        if not labels:
            continue
        label = node_text(labels[0], " ")
        values = [node_text(dd) for dd in dl.xpath("./dd")]
        fields.append({"label": label, "values": values})

    if not fields:
        raise ScrapeError(f"No information fields found on detail page {url}")

    filing_rows = doc.xpath('//*[@id="tblOrderForm"]//tr[td]')
    renewal_rows = doc.xpath('//*[@id="profile2"]//table//tr[td]')
    detail_name_nodes = doc.xpath(
        '//div[contains(concat(" ", normalize-space(@class), " "), " business-name ")]'
        '//*[contains(concat(" ", normalize-space(@class), " "), " navbar-brand ")]'
    )
    detail_name = node_text(detail_name_nodes[0], " ") if detail_name_nodes else ""
    return {
        "detail_business_name": detail_name,
        "fields": fields,
        "filing_history": parse_history_rows(filing_rows),
        "renewal_history": parse_history_rows(renewal_rows, renewal=True),
    }


def drop_one_suffix(name: str) -> str | None:
    match = SUFFIX_RE.match(name)
    if not match:
        return None
    canonical_suffix = match.group("suffix").replace(".", "").upper()
    if canonical_suffix not in CORPORATE_SUFFIXES:
        return None
    return match.group("stem").rstrip(" ,.")


def scrape_name(client: PoliteClient, index: int, name: str) -> dict[str, Any]:
    shortened = drop_one_suffix(name)
    attempts: list[dict[str, Any]] = []
    selected: tuple[str, str, bool, list[dict[str, Any]]] | None = None

    candidates = [(name, False)]
    if shortened:
        candidates.append((shortened, True))

    for query_name, suffix_dropped in candidates:
        for status in ("Active", "Inactive"):
            url = search_url(query_name, status)
            results = parse_search_page(client.get(url), status)
            attempts.append(
                {
                    "search_name": query_name,
                    "filing_status": status,
                    "suffix_dropped": suffix_dropped,
                    "result_count": len(results),
                    "url": url,
                }
            )
            if results:
                selected = (query_name, status, suffix_dropped, results)
                break
        if selected:
            break

    record: dict[str, Any] = {
        "index": index,
        "name": name,
        "searched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "attempts": attempts,
        "suffix_drop_applicable": shortened is not None,
    }
    if not selected:
        record.update(
            {
                "matched": False,
                "filing_status_used": None,
                "search_name_used": None,
                "suffix_dropped": False,
                "result_count": 0,
                "matches": [],
            }
        )
        return record

    query_name, status, suffix_dropped, results = selected
    opened: list[dict[str, Any]] = []
    for result in results[:3]:
        try:
            detail = parse_detail_page(client.get(result["url"]), result["url"])
            opened.append({**result, **detail})
        except ScrapeError as exc:
            # A small number of legacy records can have permanently broken detail
            # pages even while the search endpoint is healthy. Preserve the match
            # and its URL after the required retries so one server-side 500 cannot
            # prevent every later input name from being searched.
            error = str(exc)
            print(
                f"WARNING: Detail page unavailable after retries for {result['url']}: {error}",
                file=sys.stderr,
                flush=True,
            )
            opened.append(
                {
                    **result,
                    "detail_error": error,
                    "fields": [],
                    "filing_history": [],
                    "renewal_history": [],
                }
            )
    record.update(
        {
            "matched": True,
            "filing_status_used": status,
            "search_name_used": query_name,
            "suffix_dropped": suffix_dropped,
            "result_count": len(results),
            "matches": opened,
        }
    )
    return record


def load_names(path: Path) -> list[str]:
    names = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()]
    if any(not name for name in names):
        empty = next(i for i, name in enumerate(names, 1) if not name)
        raise ScrapeError(f"Blank input name at line {empty}")
    if len(names) != EXPECTED_NAME_COUNT:
        raise ScrapeError(
            f"Expected {EXPECTED_NAME_COUNT:,} input names, found {len(names):,}"
        )
    if len(set(names)) != len(names):
        raise ScrapeError("Input contains an exact duplicate name")
    return names


def load_checkpoint(path: Path, names: list[str]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    good_bytes = 0
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                good_bytes += len(raw)
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                if line_number == sum(1 for _ in path.open("rb")):
                    backup = path.with_suffix(path.suffix + ".partial.bak")
                    backup.write_bytes(path.read_bytes())
                    with path.open("r+b") as repair:
                        repair.truncate(good_bytes)
                    print(f"Removed incomplete trailing checkpoint line; backup: {backup}", file=sys.stderr)
                    break
                raise ScrapeError(f"Invalid JSON in checkpoint line {line_number}")
            records.append(record)
            good_bytes += len(raw)

    for expected_index, record in enumerate(records, 1):
        if record.get("index") != expected_index:
            raise ScrapeError(
                f"Checkpoint index mismatch: expected {expected_index}, found {record.get('index')}"
            )
        if expected_index > len(names) or record.get("name") != names[expected_index - 1]:
            raise ScrapeError(f"Checkpoint/input mismatch at name {expected_index}")
    return records


def append_checkpoint(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def format_field(field: dict[str, Any]) -> str:
    values = [clean(str(value)) for value in field.get("values", [])]
    return f"      {field.get('label', '')}: {'; '.join(values)}".rstrip()


def format_record(record: dict[str, Any]) -> str:
    lines = [f"{record['index']}. {record['name'].upper()}"]
    if not record.get("matched"):
        if record.get("suffix_drop_applicable"):
            lines.append(
                "   No results (Begins With, Active and Inactive, suffix-drop attempted)."
            )
        else:
            lines.append(
                "   No results (Begins With, Active and Inactive; suffix-drop not applicable)."
            )
        return "\n".join(lines)

    lines.append(
        f"   Filing Status used: {record['filing_status_used']}; "
        f"{record['result_count']} match(es) returned"
    )
    if record.get("suffix_dropped"):
        lines.append(f"   Suffix-drop search string used: {record['search_name_used']}")
    for match_number, match in enumerate(record.get("matches", []), 1):
        lines.append(f"Match {match_number}: {match['business_name']}")
        lines.append(f"   {match['status_line']}")
        if match.get("prior_name_hit"):
            lines.append("   Prior-name hit: Yes")
        lines.append(f"   {match['url']}")
        if match.get("detail_error"):
            lines.append(
                "      Detail page unavailable after 4 attempts: "
                f"{match['detail_error']}"
            )
            continue
        for field in match.get("fields", []):
            lines.append(format_field(field))
        lines.append("      Filing History:")
        history = match.get("filing_history", [])
        lines.extend(f"         {entry}" for entry in history) if history else lines.append("         None shown")
        lines.append("      Renewal History:")
        renewals = match.get("renewal_history", [])
        lines.extend(f"         {entry}" for entry in renewals) if renewals else lines.append("         None shown")
    return "\n".join(lines)


def write_text_report(path: Path, records: list[dict[str, Any]], pilot: bool = False) -> None:
    matched = sum(bool(record.get("matched")) for record in records)
    no_results = len(records) - matched
    title = "Minnesota Secretary of State - Business Search Results"
    scope_note = "Pilot: first 25 names only." if pilot else ""
    header = [
        title,
        f"Source list: {SOURCE_DESCRIPTION}",
        (
            "Search rules: Scope = Begins With; Include Prior Names = Include; Filing Status = "
            "Active first, Inactive if no active results; suffix-drop fallback; first up to 3 "
            "matches opened per name."
        ),
        f"Searched {len(records):,} names: {matched:,} with matches, {no_results:,} with no results (Active or Inactive).",
        f"Portal: {PORTAL_URL}  Generated: {dt.date.today().isoformat()}",
    ]
    if scope_note:
        header.append(scope_note)
    body = "\n\n".join(format_record(record) for record in records)
    path.write_text("\n".join(header) + "\n\n" + body + "\n", encoding="utf-8", newline="\n")


def git_checkpoint(repo: Path, files: list[Path], last_index: int, push: bool) -> None:
    relative = [str(path.relative_to(repo)) for path in files if path.exists()]
    if not relative:
        return
    subprocess.run(["git", "add", "--", *relative], cwd=repo, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo)
    if staged.returncode == 0:
        return
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            f"Checkpoint MNDPS SOS scrape through name {last_index} of {EXPECTED_NAME_COUNT}",
        ],
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
    parser.add_argument("--limit", type=int, default=EXPECTED_NAME_COUNT)
    parser.add_argument("--delay", type=float, default=1.05)
    parser.add_argument("--checkpoint-every", type=int, default=100)
    parser.add_argument("--push-checkpoints", action="store_true")
    parser.add_argument("--pilot-output", type=Path)
    args = parser.parse_args()

    repo = Path.cwd().resolve()
    input_path = (repo / args.input).resolve() if not args.input.is_absolute() else args.input
    checkpoint_path = (
        (repo / args.checkpoint).resolve() if not args.checkpoint.is_absolute() else args.checkpoint
    )
    names = load_names(input_path)
    records = load_checkpoint(checkpoint_path, names)
    target = min(args.limit, len(names))
    if len(records) > target:
        raise ScrapeError(
            f"Checkpoint already contains {len(records)} records, beyond requested limit {target}"
        )

    print(f"Validated {len(names):,} names; resuming at #{len(records) + 1}; target #{target}", flush=True)
    client = PoliteClient(minimum_delay=args.delay)
    try:
        for index in range(len(records) + 1, target + 1):
            started = time.monotonic()
            record = scrape_name(client, index, names[index - 1])
            append_checkpoint(checkpoint_path, record)
            records.append(record)
            elapsed = time.monotonic() - started
            state = (
                f"{record['filing_status_used']} / {record['result_count']} matches"
                if record.get("matched")
                else "no results"
            )
            print(
                f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                f"{index}/{target} {names[index - 1]} -> {state} ({elapsed:.1f}s)",
                flush=True,
            )
            if index % args.checkpoint_every == 0 and index < target:
                git_checkpoint(repo, [checkpoint_path], index, args.push_checkpoints)
    except BaseException:
        if records:
            git_checkpoint(repo, [checkpoint_path], records[-1]["index"], args.push_checkpoints)
        raise

    extra_files = [checkpoint_path]
    if args.pilot_output:
        pilot_path = (
            (repo / args.pilot_output).resolve()
            if not args.pilot_output.is_absolute()
            else args.pilot_output
        )
        write_text_report(pilot_path, records[:target], pilot=True)
        extra_files.append(pilot_path)
        print(f"Pilot output written: {pilot_path}", flush=True)
    if records:
        git_checkpoint(repo, extra_files, records[-1]["index"], args.push_checkpoints)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ScrapeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2)
