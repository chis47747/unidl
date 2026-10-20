"""Length-preserving Blowfish transport decoding for complete media files."""

from Crypto.Cipher import Blowfish

_BLOCK_BYTES = 2048
_STRIPE_BYTES = 3 * _BLOCK_BYTES
_DEFAULT_IV = bytes(range(8))


def decrypt_stripes(data: bytes, key: bytes, iv: bytes | None = None) -> bytes:
    """Decode every third complete block, restarting CBC for each stripe.

    The input must begin at byte zero of a complete file. A short final block
    is clear transport data, not CBC padding, and must remain unchanged.
    """
    if iv is None:
        iv = _DEFAULT_IV
    if len(key) != 16 or len(iv) != 8:
        raise ValueError("BF_CBC_STRIPE requires a 16-byte key and an 8-byte IV.")

    decoded = bytearray(data)
    for position in range(0, len(data) - _BLOCK_BYTES + 1, _STRIPE_BYTES):
        end = position + _BLOCK_BYTES
        cipher = Blowfish.new(key, Blowfish.MODE_CBC, iv=iv)
        decoded[position:end] = cipher.decrypt(data[position:end])
    return bytes(decoded)
