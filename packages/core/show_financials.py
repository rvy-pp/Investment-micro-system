"""Print the P3 valuation inputs and where each came from."""

import pathlib
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
d = yaml.safe_load((REPO / "specs" / "entities" / "base_financials.yaml")
                   .read_text(encoding="utf-8"))

print(f"{'company':16}{'EBITDA cr':>11}{'shares bn':>11}{'net debt':>10}"
      f"{'metric':>12}  provenance")
print("-" * 78)
for k, v in d["companies"].items():
    s = v.get("shares_outstanding")
    sh = f"{s/1e9:.3f}" if s else "—"
    nd = v.get("net_debt")
    prov = f"shares={v.get('shares_verify','—')} debt={v.get('net_debt_verify','—')}"
    print(f"{k:16}{v.get('base_ebitda', 0):>11,}{sh:>11}"
          f"{('—' if nd is None else nd):>10}{v.get('valuation_metric','—'):>12}  {prov}")
sys.exit(0)
