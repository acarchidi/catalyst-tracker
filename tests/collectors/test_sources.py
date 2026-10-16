"""Each collector on recorded responses. Expected values were read by hand from the fixture files."""

import json
from datetime import UTC, date, datetime

import pytest

from catalyst.collectors.http import RobotsDisallowedError
from catalyst.collectors.run import build_clients
from catalyst.collectors.sec.edgar import ExhibitRef, FilingRef, list_documents
from catalyst.collectors.sec.filings import collect_filings, documents_to_keep
from catalyst.collectors.sources import ctgov, fedreg, openfda, wires
from catalyst.schemas import SourceTier
from conftest import RecordedServer

EMAIL = "data@example.org"


def test_trials_follow_pages_and_keep_only_exact_lead_sponsor_matches() -> None:
    server = RecordedServer("ctgov-insmed")
    clients = build_clients(EMAIL, server.transport, rate_limits=False)
    documents = list(
        ctgov.collect_sponsor_studies(
            clients.web, frozenset({"Insmed Incorporated"}), max_pages=2, page_size=5
        )
    )
    assert len(server.requested) == 2
    keys = [d.source_key for d in documents]
    assert len(keys) == 9
    assert not any("NCT04817332" in k for k in keys)  # lead sponsor: University of Dundee
    first = documents[0]
    assert first.source_key == "ctgov:NCT04677543:2024-06-26"
    assert first.url == "https://clinicaltrials.gov/study/NCT04677543"
    assert first.source_tier is SourceTier.REGISTRY
    assert first.published_at == datetime(2024, 6, 28, 4, 0, tzinfo=UTC)  # midnight New York (EDT)
    assert json.loads(first.content)["protocolSection"]["identificationModule"]["nctId"] == "NCT04677543"


def test_fda_applications_are_keyed_by_their_latest_action() -> None:
    clients = build_clients(EMAIL, RecordedServer("openfda-insmed").transport, rate_limits=False)
    documents = list(openfda.collect_applications(clients.web, "Insmed", frozenset({"INSMED INC"})))
    assert [d.source_key for d in documents] == [
        "openfda:NDA207356:20260625:7",
        "openfda:NDA217673:20250812:1",
    ]
    assert documents[0].url.endswith("ApplNo=207356")
    assert documents[0].published_at == datetime(2026, 6, 25, 4, 0, tzinfo=UTC)
    assert documents[0].source_tier is SourceTier.REGULATOR_OR_SEC


def test_fda_applications_from_another_sponsor_are_not_linked() -> None:
    clients = build_clients(EMAIL, RecordedServer("openfda-insmed").transport, rate_limits=False)
    assert list(openfda.collect_applications(clients.web, "Insmed", frozenset({"SOMEONE ELSE INC"}))) == []


def test_only_advisory_committee_meeting_notices_are_kept() -> None:
    clients = build_clients(EMAIL, RecordedServer("fedreg-2026-08").transport, rate_limits=False)
    documents = list(fedreg.collect_advisory_committee_notices(clients.web, date(2026, 8, 1)))
    # 15 FDA notices mention advisory committees; 4 are meeting notices (renewals and nominations are not).
    assert [d.source_key for d in documents] == [
        "fedreg:2026-19700",
        "fedreg:2026-18920",
        "fedreg:2026-18349",
        "fedreg:2026-16245",
    ]
    assert all(d.url.startswith("https://www.federalregister.gov/documents/2026/") for d in documents)
    assert documents[0].published_at == datetime(2026, 9, 25, 4, 0, tzinfo=UTC)


def test_wire_feed_items_are_keyed_by_guid_after_checking_robots() -> None:
    server = RecordedServer("wires-prnewswire")
    clients = build_clients(EMAIL, server.transport, rate_limits=False)
    documents = list(wires.collect_feed(clients.web, wires.FEEDS[0]))
    assert server.requested[0] == "https://www.prnewswire.com/robots.txt"
    assert len(documents) == 20
    first = documents[0]
    link = "https://www.prnewswire.com/news-releases/n6-achieves-iso-134852016-certification-strengthening-the-quality-foundation-behind-iconpcr-instruments-302911426.html"
    assert first.source_key == f"wire:prnewswire:{link}"
    assert first.url == link
    assert first.published_at == datetime(2026, 10, 16, 16, 4, tzinfo=UTC)
    assert first.source_tier is SourceTier.WIRE


def test_a_feed_that_robots_txt_disallows_is_not_fetched() -> None:
    server = RecordedServer(
        "wires-prnewswire",
        overrides={"https://www.prnewswire.com/robots.txt": (200, b"User-agent: *\nDisallow: /rss/\n")},
    )
    clients = build_clients(EMAIL, server.transport, rate_limits=False)
    with pytest.raises(RobotsDisallowedError):
        list(wires.collect_feed(clients.web, wires.FEEDS[0]))
    assert server.requested == ["https://www.prnewswire.com/robots.txt"]


def _filing(form: str, accession: str) -> FilingRef:
    return FilingRef(
        cik=875320,
        accession_number=accession,
        form=form,
        filed_on=date(2026, 8, 4),
        accepted_at=datetime(2026, 8, 4, 20, 0, tzinfo=UTC),
        primary_document="x.htm",
    )


def test_a_10q_keeps_only_its_main_report() -> None:
    server = RecordedServer("sec-vertex-10q-index")
    index_url = next(iter(server.responses))
    documents = list_documents(server.responses[index_url][1].decode())
    kept = documents_to_keep(_filing("10-Q", "0000875320-26-000259"), documents)
    assert [d.url for d in kept] == [
        "https://www.sec.gov/Archives/edgar/data/875320/000087532026000259/vrtx-20260630.htm"
    ]


def test_an_8k_keeps_every_exhibit_99() -> None:
    documents = [
        ExhibitRef(type="8-K", description="", url="https://www.sec.gov/a/8k.htm"),
        ExhibitRef(type="EX-99.1", description="", url="https://www.sec.gov/a/ex991.htm"),
        ExhibitRef(type="EX-99.2", description="", url="https://www.sec.gov/a/ex992.htm"),
        ExhibitRef(type="EX-10.1", description="", url="https://www.sec.gov/a/ex101.htm"),
    ]
    kept = documents_to_keep(_filing("8-K", "0000875320-26-000256"), documents)
    assert [d.type for d in kept] == ["EX-99.1", "EX-99.2"]


def test_sec_filings_since_a_date_with_already_stored_filings_skipped() -> None:
    server = RecordedServer("sec/875320")
    clients = build_clients(EMAIL, server.transport, rate_limits=False)
    submissions = server.responses["https://data.sec.gov/submissions/CIK0000875320.json"][1]
    fresh = list(
        collect_filings(clients.sec, submissions, date(2026, 8, 1), lambda _: False, frozenset({"8-K"}))
    )
    assert [d.source_key for d in fresh] == ["sec:0000875320-26-000256:ex-991_q22026.htm"]
    server.requested.clear()
    stored = list(
        collect_filings(
            clients.sec, submissions, date(2026, 8, 1), lambda prefix: "000256" in prefix, frozenset({"8-K"})
        )
    )
    assert stored == []
    assert not any("000087532026000256" in url for url in server.requested)


@pytest.mark.parametrize(("status", "allowed"), [(404, True), (503, False)])
def test_robots_txt_missing_allows_and_unreadable_blocks(status: int, allowed: bool) -> None:
    server = RecordedServer(
        "wires-prnewswire", overrides={"https://www.prnewswire.com/robots.txt": (status, b"")}
    )
    clients = build_clients(EMAIL, server.transport, rate_limits=False)
    if allowed:
        assert len(list(wires.collect_feed(clients.web, wires.FEEDS[0]))) == 20
    else:
        with pytest.raises(RobotsDisallowedError):
            list(wires.collect_feed(clients.web, wires.FEEDS[0]))


def test_hosts_outside_the_list_are_refused() -> None:
    clients = build_clients(EMAIL, RecordedServer().transport, rate_limits=False)
    with pytest.raises(ValueError, match="only fetches from"):
        clients.web.get("https://www.globenewswire.com/rss")
