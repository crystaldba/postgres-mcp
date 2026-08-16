"""Token benchmark for the opt-in GCF response encoding.

Compares the tool-result text a row-shaped query produces as compact JSON vs as a GCF
generic-profile block (what ``--response-format=gcf`` emits), using the server's own
``gcf_format.encode_rows`` path so the numbers reflect the real never-grow / lossless
guards. Token counts use the o200k_base tokenizer (GPT-4o / GPT-5 family) when tiktoken
is installed; byte counts are always reported.

Run:  python benchmarks/gcf_benchmark.py
Fixtures under benchmarks/fixtures/ are real query output captured from a Postgres
instance (a 200-row table SELECT and an information_schema.columns listing).
"""

import json
import pathlib

from postgres_mcp import gcf_format

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("o200k_base")

    def toks(s: str) -> int:
        return len(_ENC.encode(s))
except Exception:  # tiktoken optional

    def toks(s: str) -> int:
        return -1


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def main() -> None:
    print(f"{'fixture':<16}{'rows':>6}{'JSON tok':>10}{'GCF tok':>10}{'vs JSON':>10}{'lossless':>10}")
    for path in sorted(FIXTURES.glob("*.json")):
        rows = json.loads(path.read_text())
        safe = json.loads(json.dumps(rows, default=str))
        json_text = json.dumps(safe, separators=(",", ":"))
        wire = gcf_format.encode_rows(rows)
        if wire is None:
            print(f"{path.stem:<16}{len(rows):>6}{toks(json_text):>10}{'-':>10}{'declined':>10}{'-':>10}")
            continue
        import gcf

        lossless = gcf.decode_generic(wire) == safe
        jt, gt = toks(json_text), toks(wire)
        pct = f"{(jt - gt) / jt * 100:.1f}%" if jt > 0 else "n/a"
        print(f"{path.stem:<16}{len(rows):>6}{jt:>10}{gt:>10}{pct:>10}{('yes' if lossless else 'NO'):>10}")


if __name__ == "__main__":
    main()
