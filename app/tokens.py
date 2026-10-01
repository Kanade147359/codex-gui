"""Token estimates. Codex itself approximates tokens as UTF-8 bytes / 4 (its truncation and `original_token_count`
use that rule), so the GUI uses the same one for anything it has to guess. Estimates are labelled as such in the UI."""
import math


def est_tokens(text) -> int:
    if not text:
        return 0
    return math.ceil(len(text.encode("utf-8", errors="replace")) / 4)


def est_tokens_bytes(n_bytes: int) -> int:
    return math.ceil(max(n_bytes, 0) / 4)
