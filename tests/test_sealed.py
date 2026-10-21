"""Sealing a file to a one-off public key, so it can pass through a public job log (ADR 0034)."""

import base64
import json
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from hypothesis import given, settings
from hypothesis import strategies as st

from catalyst import cli
from catalyst.sealed import BEGIN, END, SealError, generate_keypair, public_line, seal, unseal

SECRET = b'{"held": [{"quote": "unchecked words that must not reach a public log"}]}'


@pytest.fixture(scope="module")
def keys() -> tuple[bytes, str]:
    return generate_keypair()


def test_a_sealed_file_opens_with_its_private_key(keys: tuple[bytes, str]) -> None:
    private, public = keys
    sealed = seal(SECRET, public)
    assert unseal(sealed, private) == SECRET


def test_the_sealed_text_holds_nothing_of_the_file(keys: tuple[bytes, str]) -> None:
    _, public = keys
    sealed = seal(SECRET, public)
    assert sealed.startswith(BEGIN + "\n")
    assert sealed.endswith(END + "\n")
    assert all(line.isascii() and len(line) <= 76 for line in sealed.splitlines())
    for word in ("held", "quote", "unchecked", "public log"):
        assert word not in sealed
        assert base64.b64encode(word.encode()).decode()[:-2] not in sealed


def test_each_seal_is_different(keys: tuple[bytes, str]) -> None:
    _, public = keys
    assert seal(SECRET, public) != seal(SECRET, public)


def test_a_job_log_with_time_stamps_opens_directly(keys: tuple[bytes, str]) -> None:
    private, public = keys
    lines = seal(SECRET, public).splitlines()
    log = "\n".join(
        [
            "2026-10-20T12:00:00.0000000Z ##[group]Export (sealed)",
            *(f"2026-10-20T12:00:01.{i:07d}Z {line}" for i, line in enumerate(lines)),
            "2026-10-20T12:00:02.0000000Z ##[endgroup]",
        ]
    )
    assert unseal(log, private) == SECRET


def test_another_key_cannot_open_it(keys: tuple[bytes, str]) -> None:
    _, public = keys
    other_private, _ = generate_keypair()
    with pytest.raises(SealError, match="does not open"):
        unseal(seal(SECRET, public), other_private)


def test_a_changed_byte_is_detected(keys: tuple[bytes, str]) -> None:
    private, public = keys
    lines = seal(SECRET, public).splitlines()
    last = lines[-2]
    lines[-2] = last[:-3] + ("A" if last[-3] != "A" else "B") + last[-2:]
    with pytest.raises(SealError):
        unseal("\n".join(lines), private)


@pytest.mark.parametrize(
    "text",
    ["", "no markers here", BEGIN + "\nAAAA\n", f"{BEGIN}\n{END}\n", f"{BEGIN}\n!!!!\n{END}\n"],
)
def test_broken_sealed_text_fails_plainly(keys: tuple[bytes, str], text: str) -> None:
    with pytest.raises(SealError):
        unseal(text, keys[0])


def test_two_sealed_files_in_one_text_are_refused(keys: tuple[bytes, str]) -> None:
    private, public = keys
    with pytest.raises(SealError, match="more than one"):
        unseal(seal(SECRET, public) + seal(b"other", public), private)


@pytest.mark.parametrize("public", ["", "not base64 at all!", base64.b64encode(b"not a key").decode()])
def test_a_bad_public_key_is_refused(public: str) -> None:
    with pytest.raises(SealError):
        seal(SECRET, public)


def test_a_short_rsa_key_is_refused() -> None:
    small = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(SealError, match="at least 3072"):
        seal(SECRET, public_line(small.public_key()))


def test_a_key_that_is_not_rsa_is_refused() -> None:
    der = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    with pytest.raises(SealError, match="not an RSA key"):
        seal(SECRET, base64.b64encode(der).decode())


def test_the_public_key_is_one_line(keys: tuple[bytes, str]) -> None:
    _, public = keys
    assert "\n" not in public
    assert public.isascii()


@settings(max_examples=25, deadline=None)
@given(st.binary(max_size=5000))
def test_any_bytes_come_back_unchanged(keys: tuple[bytes, str], data: bytes) -> None:
    private, public = keys
    assert unseal(seal(data, public), private) == data


# --- The commands the workflow and the coding session use ---


def test_the_commands_seal_and_open_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key = tmp_path / "export.key"
    assert cli.main(["seal-keygen", "--private-key", str(key)]) == 0
    public = capsys.readouterr().out.strip()
    assert stat.S_IMODE(key.stat().st_mode) == 0o600  # only its owner can read it
    plain = tmp_path / "export.json"
    plain.write_bytes(SECRET)
    assert cli.main(["seal", "--to", public, "--in", str(plain)]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith(BEGIN)
    assert "unchecked" not in printed
    sealed = tmp_path / "job.log"
    sealed.write_text(printed, encoding="utf-8")
    opened = tmp_path / "opened.json"
    assert cli.main(["unseal", "--private-key", str(key), "--in", str(sealed), "--out", str(opened)]) == 0
    assert opened.read_bytes() == SECRET
    assert json.loads(capsys.readouterr().out) == {"out": str(opened), "bytes": len(SECRET)}


def test_keygen_never_writes_over_a_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key = tmp_path / "export.key"
    key.write_text("an older key", encoding="utf-8")
    assert cli.main(["seal-keygen", "--private-key", str(key)]) == 1
    assert key.read_text(encoding="utf-8") == "an older key"
    assert capsys.readouterr().out == ""


def test_a_bad_public_key_prints_nothing_of_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "export.json"
    plain.write_bytes(SECRET)
    assert cli.main(["seal", "--to", "not-a-key", "--in", str(plain)]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "unchecked" not in err
    assert "Nothing sealed" in err


def test_unseal_never_writes_over_a_file(
    keys: tuple[bytes, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private, public = keys
    key, sealed, opened = tmp_path / "k", tmp_path / "s", tmp_path / "o"
    key.write_bytes(private)
    sealed.write_text(seal(SECRET, public), encoding="utf-8")
    opened.write_text("keep me", encoding="utf-8")
    assert cli.main(["unseal", "--private-key", str(key), "--in", str(sealed), "--out", str(opened)]) == 1
    assert opened.read_text(encoding="utf-8") == "keep me"
    assert "already exists" in capsys.readouterr().err
