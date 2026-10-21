"""Seal a file to a public key, so it can pass through a public job log (ADR 0034, amended).

The repository is public since 2026-10-20, so its job logs are public too. The private export
holds claims the publish gate has not let through, so it may only leave a job sealed: encrypted
to a one-off public key whose private half never leaves the coding session.

The scheme is standard hybrid encryption from the `cryptography` package, nothing new:
- a fresh 256-bit AES key per file, used once with AES-GCM (which also detects any change);
- that key wrapped with RSA-OAEP (SHA-256) to the recipient's RSA key of at least 3072 bits;
- the format line and the wrapped key are bound to the ciphertext as associated data.

The public key travels as one line of text (base64 of its DER SubjectPublicKeyInfo), so it fits
a workflow input. The sealed file is ASCII between two marker lines; `unseal` also reads it
straight from a downloaded job log, whose lines start with a time stamp.
"""

import base64
import binascii
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT = b"catalyst-sealed/1\n"
BEGIN = "-----BEGIN CATALYST SEALED FILE-----"
END = "-----END CATALYST SEALED FILE-----"
KEY_BITS = 4096
MIN_KEY_BITS = 3072
NONCE_BYTES = 12
LINE = 76
_OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


class SealError(ValueError):
    """A key or sealed file that cannot be used; the message never holds the file's contents."""


def generate_keypair() -> tuple[bytes, str]:
    """Return a new private key (PEM, unencrypted, for a file only its owner can read) and its public line."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return pem, public_line(private.public_key())


def public_line(key: rsa.RSAPublicKey) -> str:
    """Return the public key as one line: base64 of its DER SubjectPublicKeyInfo."""
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode("ascii")


def load_public_line(text: str) -> rsa.RSAPublicKey:
    """Read a public line; refuse anything but an RSA key of at least MIN_KEY_BITS bits."""
    try:
        der = base64.b64decode(text.strip(), validate=True)
        key = serialization.load_der_public_key(der)
    except (binascii.Error, ValueError) as error:
        raise SealError("the public key is not a base64 DER public key") from error
    if not isinstance(key, rsa.RSAPublicKey):
        raise SealError("the public key is not an RSA key")
    if key.key_size < MIN_KEY_BITS:
        raise SealError(f"the public key has {key.key_size} bits; at least {MIN_KEY_BITS} are needed")
    return key


def seal(plaintext: bytes, public: str) -> str:
    """Return `plaintext` sealed to the public line, as ASCII between the marker lines."""
    key = load_public_line(public)
    data_key = AESGCM.generate_key(bit_length=256)
    wrapped = key.encrypt(data_key, _OAEP)
    header = FORMAT + struct.pack(">H", len(wrapped)) + wrapped
    nonce = os.urandom(NONCE_BYTES)
    body = header + nonce + AESGCM(data_key).encrypt(nonce, plaintext, header)
    text = base64.b64encode(body).decode("ascii")
    lines = [text[i : i + LINE] for i in range(0, len(text), LINE)]
    return "\n".join([BEGIN, *lines, END]) + "\n"


def _armored_body(sealed: str) -> bytes:
    """Return the decoded bytes between the markers; each line may carry a log time stamp before it."""
    inside = done = False
    parts: list[str] = []
    for raw in sealed.splitlines():
        line = raw.rstrip()
        if line.endswith(BEGIN):
            if inside or done:
                raise SealError("the text holds more than one sealed file")
            inside = True
        elif line.endswith(END) and inside:
            inside, done = False, True
        elif inside:
            words = line.split()
            parts.append(words[-1] if words else "")
    if not done or not parts:
        raise SealError("no complete sealed file was found between the marker lines")
    try:
        return base64.b64decode("".join(parts), validate=True)
    except binascii.Error as error:
        raise SealError("the sealed file is not valid base64") from error


def unseal(sealed: str, private_pem: bytes) -> bytes:
    """Return the original bytes; fail if the key is wrong or a single byte was changed."""
    try:
        private = serialization.load_pem_private_key(private_pem, password=None)
    except (ValueError, TypeError) as error:
        raise SealError("the private key could not be read") from error
    if not isinstance(private, rsa.RSAPrivateKey):
        raise SealError("the private key is not an RSA key")
    body = _armored_body(sealed)
    start = len(FORMAT)
    if not body.startswith(FORMAT) or len(body) < start + 2:
        raise SealError("the sealed file is not in a known format")
    (size,) = struct.unpack(">H", body[start : start + 2])
    header_end = start + 2 + size
    if len(body) < header_end + NONCE_BYTES + 16:
        raise SealError("the sealed file is cut short")
    header, nonce = body[:header_end], body[header_end : header_end + NONCE_BYTES]
    try:
        data_key = private.decrypt(body[start + 2 : header_end], _OAEP)
        return AESGCM(data_key).decrypt(nonce, body[header_end + NONCE_BYTES :], header)
    except (ValueError, InvalidTag) as error:
        raise SealError("the sealed file does not open with this key, or it was changed") from error
