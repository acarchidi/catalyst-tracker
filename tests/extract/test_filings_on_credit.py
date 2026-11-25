"""Filings on the promotional credit (owner, in writing, 2026-11-25; ADR 0081), on a disposable database.

- Filings are read newest first by filing date, 10-Q before 10-K before other exhibits on the
  same date, and a started filing still comes first.
- Until 2026-12-08, while the credit check holds, a filing part starts only while the role's whole
  spend today plus that part's worst case stays within its share (half) of the raised cap.
- Every filing call is a `model_call` row, so it counts toward the $96 credit check.
- From 2026-12-09 filings are back at the usual $2 caps with no change to the file.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.db.tables import TABLES
from catalyst.extract.backfill import CREDIT_FIRST_DAY, CREDIT_USD, DAY_MOST_USD, credit_holds, spent_since
from catalyst.extract.filings import SHARE_STOP, USUAL_CAP_STOP, SpendGuard, part_call_most, read_filings
from catalyst.llm.client import Completion, ModelClient
from catalyst.llm.config import DEFAULT_CONFIG_PATH, ModelConfig, RaisedCap, RoleConfig, load_config
from catalyst.llm.ledger import DatabaseLedger, InMemoryLedger
from catalyst.llm.pricing import Price, TokenUsage
from catalyst.llm.prompts import Prompt
from catalyst.schemas import ModelCall, ModelCallStatus, SourceTier
from catalyst.snapshots import LocalSnapshotStore
from extract.filing_pages import ANNUAL, QUARTERLY

NOW = datetime(2026, 11, 26, 6, tzinfo=UTC)  # 01:00 in New York
TODAY = date(2026, 11, 26)
LAST_DAY = date(2026, 12, 8)
EXHIBIT = b"<html><body><p>" + b"Acme Bio presents new data at a medical meeting. " * 20 + b"</p></body></html>"


@dataclass
class _Fake:
    """Answers with no catalyst, and remembers what it was shown."""

    name: str = "fake"
    shown: list[str] = field(default_factory=list[str])

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        self.shown.append(document)
        return Completion(
            json.dumps({"catalysts": []}), model, "req", "end_turn", TokenUsage(input=500, output=100)
        )


def _config(share: str | None = "0.5", cap: str = "2", raised: str = "8") -> ModelConfig:
    price = Price(input=Decimal(2), cache_write=Decimal(3), cache_read=Decimal(0), output=Decimal(10))
    extra: dict[str, object] = {}
    if share is not None:
        extra = {"filings_share": Decimal(share), "filings_approved": "owner, in writing, 2026-11-25"}
    raises = (
        RaisedCap.model_validate(
            {"cap_usd": Decimal(raised), "first_day": date(2026, 11, 24), "last_day": LAST_DAY}
            | {"approved": "owner, 2026-11-23"}
            | extra
        ),
    )
    role = RoleConfig(
        provider="fake", model="claude-sonnet-5-5", max_tokens=1000, daily_cap_usd=Decimal(cap), raised_caps=raises
    )
    return ModelConfig(
        version="test", pricing={"claude-sonnet-5-5": price}, roles={"extractor": role, "verifier": role}
    )


def _store(
    factory: sessionmaker[Session],
    store: LocalSnapshotStore,
    key: str,
    kind: str,
    published: datetime,
    content: bytes = QUARTERLY,
) -> str:
    fetched = FetchedDocument(
        source_key=f"sec:0000000001-26-{key}:{key}.htm",
        url=f"https://www.sec.gov/Archives/edgar/data/1/{key}.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type=kind,
        published_at=published,
        content=content,
        content_type="text/html",
    )
    store_fetched(fetched, store, factory)
    return fetched.source_key


def _order(factory: sessionmaker[Session], store: LocalSnapshotStore, **options: object) -> list[str]:
    client = ModelClient(_config(), {"fake": _Fake()}, InMemoryLedger(), now=lambda: NOW)
    report = read_filings(factory, store, client, limit=10, now=lambda: NOW, **options)  # type: ignore[arg-type]
    return [document.source_key for document in report.documents]


def test_filings_are_read_newest_date_first_and_10_q_before_10_k_before_exhibits_on_a_date(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    day = datetime(2026, 11, 10, tzinfo=UTC)
    # Same New York date (2026-11-10); the exhibit was accepted last, the 10-Q first.
    exhibit = _store(session_factory, store, "x", "8-K EX-99.2", day + timedelta(hours=22), EXHIBIT)
    annual = _store(session_factory, store, "k", "10-K", day + timedelta(hours=20), ANNUAL)
    quarterly = _store(session_factory, store, "q", "10-Q", day + timedelta(hours=14))
    # 23:30 New York on 2026-11-10 is 04:30 UTC on 2026-11-11: still 2026-11-10's date.
    late_quarterly = _store(
        session_factory, store, "q2", "10-Q", day + timedelta(hours=28, minutes=30), QUARTERLY + b" "
    )
    newer_exhibit = _store(session_factory, store, "n", "8-K EX-99", day + timedelta(days=2), EXHIBIT + b" ")
    older_quarterly = _store(session_factory, store, "o", "10-Q", day - timedelta(days=3), QUARTERLY + b"  ")
    assert _order(session_factory, store, part_chars=80_000) == [
        newer_exhibit,
        late_quarterly,
        quarterly,
        annual,
        exhibit,
        older_quarterly,
    ]


def test_a_started_filing_still_comes_first_and_goes_on_with_its_next_part(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    older = _store(session_factory, store, "o", "10-Q", datetime(2026, 11, 3, 21, tzinfo=UTC))
    config = _config(share=None)
    ledger = InMemoryLedger()
    # Room for exactly one part under the usual $2: the first run reads one part and stops.
    ledger._extra[("extractor", TODAY)] = Decimal(2) - part_call_most(config, "extractor") - Decimal("0.001")  # pyright: ignore[reportPrivateUsage]
    client = ModelClient(config, {"fake": _Fake()}, ledger, now=lambda: NOW)
    guard = SpendGuard(config, ledger, TODAY)
    first = read_filings(session_factory, store, client, limit=10, now=lambda: NOW, guard=guard, part_chars=600)
    assert first.summary()["filing_parts_read"] == 1
    assert first.summary()["stopped_early"] == USUAL_CAP_STOP
    newer = _store(session_factory, store, "n", "10-Q", datetime(2026, 11, 20, 21, tzinfo=UTC), QUARTERLY + b" ")
    assert _order(session_factory, store, part_chars=600) == [older, newer]  # the started one first


def _spent_before(ledger: InMemoryLedger, role: str, amount: Decimal) -> None:
    ledger._extra[(role, TODAY)] = amount  # pyright: ignore[reportPrivateUsage]


def test_while_the_credit_holds_filings_spend_up_to_half_the_raised_cap_and_no_more(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store(session_factory, store, "q", "10-Q", datetime(2026, 11, 20, 21, tzinfo=UTC))
    config = _config()
    worst = part_call_most(config, "extractor")
    ledger = InMemoryLedger()
    _spent_before(ledger, "extractor", Decimal(4) - worst - Decimal("0.001"))  # past the usual $2
    fake = _Fake()
    client = ModelClient(config, {"fake": fake}, ledger, now=lambda: NOW)
    guard = SpendGuard(config, ledger, TODAY, credit_holds=True)
    assert guard.line("extractor") == Decimal(4)  # half of $8
    assert guard.line("verifier") == Decimal(4)  # the test gives both roles the same $8
    assert guard.on_credit()
    report = read_filings(session_factory, store, client, limit=10, now=lambda: NOW, guard=guard, part_chars=600)
    assert len(fake.shown) == 1  # one part fits under $4; the next would not
    assert report.summary()["stopped_early"] == SHARE_STOP
    assert ledger.spent("extractor", TODAY) + worst > Decimal(4)
    assert ledger.spent("extractor", TODAY) < config.roles["extractor"].cap_on(TODAY)  # $8 left for releases


def test_the_share_is_of_the_role_s_whole_spend_so_releases_read_earlier_count_too() -> None:
    config = _config()
    ledger = InMemoryLedger()
    _spent_before(ledger, "extractor", Decimal("3.99"))  # all of it on press releases
    guard = SpendGuard(config, ledger, TODAY, credit_holds=True)
    assert not guard.allows("extractor")
    assert guard.stop_reason() == SHARE_STOP


def test_without_the_credit_check_or_without_a_share_filings_keep_the_usual_cap() -> None:
    ledger = InMemoryLedger()
    _spent_before(ledger, "extractor", Decimal("1.95"))
    no_credit = SpendGuard(_config(), ledger, TODAY, credit_holds=False)
    assert no_credit.line("extractor") == Decimal(2)
    assert not no_credit.allows("extractor")
    assert not no_credit.on_credit()
    assert no_credit.stop_reason() == USUAL_CAP_STOP
    no_share = SpendGuard(_config(share=None), ledger, TODAY, credit_holds=True)
    assert no_share.line("extractor") == Decimal(2)
    assert not no_share.on_credit()


@pytest.mark.parametrize(
    ("day", "extractor", "verifier"),
    [
        (date(2026, 11, 23), "2.00", "2.00"),  # before the raised caps
        (date(2026, 11, 24), "4.00", "2.00"),
        (TODAY, "4.00", "2.00"),
        (LAST_DAY, "4.00", "2.00"),
        (date(2026, 12, 9), "2.00", "2.00"),  # back to the usual caps with no change to the file
        (date(2027, 1, 15), "2.00", "2.00"),
    ],
)
def test_the_committed_file_gives_filings_half_the_raised_caps_until_2026_12_08_only(
    day: date, extractor: str, verifier: str
) -> None:
    config = load_config(DEFAULT_CONFIG_PATH)
    guard = SpendGuard(config, InMemoryLedger(), day, credit_holds=True)
    assert guard.line("extractor") == Decimal(extractor)
    assert guard.line("verifier") == Decimal(verifier)
    assert guard.on_credit() is (Decimal(extractor) > Decimal(2))


def test_the_committed_share_is_what_the_owner_allowed() -> None:
    """Owner, in writing, 2026-11-25: filings on the credit, within the $8 and $4 caps, at most half."""
    roles = load_config(DEFAULT_CONFIG_PATH).roles
    for name, role in roles.items():
        for raised in role.raised_caps:
            assert raised.filings_share <= Decimal("0.5")
            if raised.filings_share:
                assert name in {"extractor", "verifier"}
                assert raised.filings_approved is not None
                assert "2026-11-25" in raised.filings_approved
                assert raised.last_day <= LAST_DAY
    assert roles["extractor"].cap_on(TODAY) == Decimal("8.00")  # caps unchanged
    assert roles["verifier"].cap_on(TODAY) == Decimal("4.00")
    assert roles["extractor"].daily_cap_usd == Decimal("2.00")
    assert roles["verifier"].daily_cap_usd == Decimal("2.00")


def test_a_share_over_half_or_without_the_owner_s_approval_is_refused() -> None:
    base = {"cap_usd": Decimal(8), "first_day": TODAY, "last_day": LAST_DAY, "approved": "owner"}
    with pytest.raises(ValueError, match="filings_share"):
        RaisedCap.model_validate(base | {"filings_share": Decimal("0.51"), "filings_approved": "owner"})
    with pytest.raises(ValueError, match="filings_approved"):
        RaisedCap.model_validate(base | {"filings_share": Decimal("0.5")})
    with pytest.raises(ValueError, match="filings_approved"):
        RaisedCap.model_validate(base | {"filings_share": Decimal("0.5"), "filings_approved": ""})
    assert RaisedCap.model_validate(base).filings_share == 0  # by default filings never use a raised cap


def test_the_file_takes_a_filings_share(tmp_path: Path) -> None:
    body = (
        'version = "t"\n[pricing.m1]\ninput = "1"\ncache_write = "1"\ncache_read = "0.1"\noutput = "5"\n'
        '[roles.extractor]\nprovider = "anthropic"\nmodel = "m1"\nmax_tokens = 100\ndaily_cap_usd = "2"\n'
        '[[roles.extractor.raised_caps]]\ncap_usd = "8"\nfirst_day = 2026-11-24\nlast_day = 2026-12-08\n'
        'approved = "owner"\nfilings_share = "0.50"\nfilings_approved = "owner, 2026-11-25"\n'
    )
    path = tmp_path / "models.toml"
    path.write_text(body)
    assert load_config(path).roles["extractor"].filings_line(LAST_DAY) == Decimal(4)
    assert load_config(path).roles["extractor"].filings_line(date(2026, 12, 9)) is None


def _spend(factory: sessionmaker[Session], amount: Decimal, at: datetime) -> None:
    call = ModelCall(
        role="verifier",
        provider="anthropic",
        model="claude-sonnet-5-5",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=amount,
        latency_ms=1,
        request_id=None,
        status=ModelCallStatus.OK,
        called_at=at,
        config_version="test",
    )
    with factory.begin() as session:
        session.execute(insert(TABLES[ModelCall]).values(**call.model_dump()))


def test_every_filing_call_counts_toward_the_credit_and_a_spent_credit_ends_the_share(
    engine: Engine, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store(session_factory, store, "q", "10-Q", datetime(2026, 11, 20, 21, tzinfo=UTC))
    ledger = DatabaseLedger(engine)
    config = _config()
    with session_factory() as session:
        assert credit_holds(session, TODAY)
        guard = SpendGuard(config, ledger, TODAY, credit_holds=credit_holds(session, TODAY))
    client = ModelClient(config, {"fake": _Fake()}, ledger, now=lambda: NOW)
    report = read_filings(session_factory, store, client, limit=10, now=lambda: NOW, guard=guard, part_chars=600)
    parts = report.summary()["filing_parts_read"]
    assert parts == 4
    with session_factory() as session:
        spent = spent_since(session, CREDIT_FIRST_DAY)
    assert spent == ledger.spent("extractor", TODAY) > 0  # the filing calls are in the credit's total
    # Spend from earlier days, up to the line: one more whole day still fits, then it does not.
    _spend(session_factory, CREDIT_USD - DAY_MOST_USD - spent, datetime(2026, 11, 24, 12, tzinfo=UTC))
    with session_factory() as session:
        assert credit_holds(session, TODAY)
    _spend(session_factory, Decimal("0.01"), datetime(2026, 11, 25, 12, tzinfo=UTC))
    with session_factory() as session:
        assert not credit_holds(session, TODAY)
        after = SpendGuard(config, ledger, TODAY, credit_holds=credit_holds(session, TODAY))
    assert after.line("extractor") == Decimal(2)  # back to the usual cap


def test_the_credit_check_ends_after_2026_12_08(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        assert credit_holds(session, LAST_DAY)
        assert not credit_holds(session, LAST_DAY + timedelta(days=1))
