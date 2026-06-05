"""Extract transactions from a credit-card statement PDF.

Two backends:
  - "cli"  (default): shells out to the local Claude Code binary, which uses your
           Claude subscription (no per-token API cost). Requires a one-time
           `claude auth login`.
  - "api"  : uses the Anthropic API (requires ANTHROPIC_API_KEY, billed per token).

Select with the SPENDING_BACKEND env var ("cli" or "api").
"""

import base64
import glob
import json
import os
import subprocess
import tempfile
from pathlib import Path

def backend():
    """Read the backend fresh each call so config set after import is respected."""
    return os.environ.get("SPENDING_BACKEND", "cli")


def _model():
    return os.environ.get("SPENDING_MODEL", "")  # "" -> backend default

CATEGORIES = [
    "Groceries", "Dining", "Transport", "Travel", "Shopping",
    "Utilities", "Entertainment", "Health", "Subscriptions",
    "Home", "Rent", "Fees", "Other",
]

INSTRUCTIONS = f"""You read a bank statement PDF and extract every transaction that is money the household actually SPENT.

The PDF may be a CREDIT CARD statement or a DEBIT / CHECKING account statement. Handle both.

Return ONLY a JSON array (no prose, no markdown fences). Each element:
{{
  "date": "YYYY-MM-DD",
  "merchant": "cleaned merchant or payee name",
  "amount": 12.34,
  "cardholder": "name of the person who made the charge / the account holder, if shown",
  "card": "issuer or bank + last 4 if shown, e.g. 'Chase 1234' or 'BofA Checking 5678'",
  "category": "one of {CATEGORIES}"
}}

INCLUDE (real spending — money leaving the household for an expense):
- Purchases and charges on a credit or debit card.
- Rent payments.
- Money SENT to another person (Venmo / Zelle / Cash App / PayPal / wire going OUT).

EXCLUDE (do NOT output these at all):
- Any INCOMING money: salary / payroll / direct deposit, interest earned, cashback or rewards, refunds, and money RECEIVED from other people.
- Credit-card payments made from a checking account (e.g. "Payment to Chase card", "AUTOPAY", "BILL PAY VISA", "ONLINE PAYMENT - AMEX"). These just pay off a card whose purchases are already recorded elsewhere; including them double-counts.
- Transfers between the household's own accounts.
- Account fees and interest charged, unless they are a real cost you want tracked.

Rules:
- amount is a POSITIVE number for money spent.
- Use the description and whether the line is a debit/withdrawal vs a credit/deposit to decide direction. Only outflows that are genuine expenses count.
- cardholder: many statements list a name next to or above each transaction (shared accounts have multiple authorized users). Use the name shown for that transaction. If there is a single account holder, use that name for all. If no name is shown, use "".
- card: identify the issuer/bank (Chase, Amex, Capital One, Citi, Bank of America, etc.) and last 4 if visible.
- category: pick the single best fit. Rent -> "Rent". Money sent to a person with no clearer purpose -> "Other". If unsure, "Other".
- date: convert any format to YYYY-MM-DD using the statement's year.
Return [] if there are no qualifying spending transactions."""


# --------------------------------------------------------------------- dispatch
def parse_pdf(pdf_bytes, client=None):
    if backend() == "api":
        return _parse_api(pdf_bytes, client)
    return _parse_cli(pdf_bytes)


# --------------------------------------------------------------------- CLI backend
def find_claude_bin():
    """Locate the bundled Claude Code binary (version-independent)."""
    override = os.environ.get("CLAUDE_BIN")
    if override and Path(override).exists():
        return override
    patterns = [
        os.path.expanduser(
            "~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude"
        ),
        os.path.expanduser("~/.claude/local/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]
    found = []
    for pat in patterns:
        found.extend(glob.glob(pat))
    if not found:
        raise RuntimeError(
            "Could not find the Claude Code binary. Set CLAUDE_BIN to its full path, "
            "or use the API backend (SPENDING_BACKEND=api)."
        )
    # Prefer the highest version path (lexical sort puts newer version dirs last).
    found.sort()
    return found[-1]


def cli_logged_in():
    try:
        out = subprocess.run(
            [find_claude_bin(), "auth", "status"],
            capture_output=True, text=True, timeout=30,
        )
        return json.loads(out.stdout or "{}").get("loggedIn") is True
    except Exception:  # noqa: BLE001
        return False


def _parse_cli(pdf_bytes):
    claude = find_claude_bin()
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "statement.pdf"
        pdf_path.write_bytes(pdf_bytes)

        prompt = (
            f"Read the PDF file at {pdf_path} and extract its transactions.\n\n"
            + INSTRUCTIONS
        )
        cmd = [
            claude, "-p", prompt,
            "--output-format", "json",
            "--allowedTools", "Read",
            "--add-dir", tmp,
            "--no-session-persistence",
        ]
        if _model():
            cmd += ["--model", _model()]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Claude CLI failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[:500]}"
            )

        # --output-format json returns an envelope; the model's text is in "result".
        text = proc.stdout.strip()
        try:
            env = json.loads(text)
            if isinstance(env, dict) and "result" in env:
                text = env["result"]
        except json.JSONDecodeError:
            pass
        return _extract_json(text)


# --------------------------------------------------------------------- API backend
def _parse_api(pdf_bytes, client):
    import anthropic

    if client is None:
        client = anthropic.Anthropic()
    b64 = base64.standard_b64encode(pdf_bytes).decode()

    last_err = None
    for attempt in range(2):
        resp = client.messages.create(
            model=_model() or "claude-opus-4-7",
            max_tokens=16000,  # long statements can produce a lot of JSON
            system=[{"type": "text", "text": INSTRUCTIONS,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "document", "source": {
                        "type": "base64", "media_type": "application/pdf", "data": b64}},
                    {"type": "text",
                     "text": "Extract all purchase transactions as the specified "
                             "JSON array. Output ONLY the JSON array, nothing else."},
                ],
            }],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()

        if resp.stop_reason == "max_tokens":
            last_err = "the statement was too long and the response got cut off"
            print(f"[parser] attempt {attempt + 1}: stop_reason=max_tokens (truncated)")
            continue
        try:
            return _extract_json(text)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = str(e)
            print(f"[parser] attempt {attempt + 1}: JSON parse failed: {e}; "
                  f"response head={text[:200]!r}")

    raise RuntimeError(
        f"Couldn't read this statement reliably ({last_err}). "
        "Please try uploading it again."
    )


# --------------------------------------------------------------------- shared
def _extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array found in the model's response")
    return json.loads(text[start : end + 1])
