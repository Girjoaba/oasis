"""Write lineitem.parquet (TPC-H dbgen) and pack it into image.bin for the RDMA mock.

Usage: prepare.py [--sf 0.01] [--pack <oasis>/rdma_server/pack_image.py]

Needs the `duckdb` Python module (pip install duckdb). The first run downloads the
tpch extension; set http_proxy/https_proxy without a trailing slash if needed.
The packer is the Oasis one, so the image has the layout of oasis_rdma_server;
without --pack the script takes the one of this checkout.
"""

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PACK = HERE.parents[1] / "rdma_server" / "pack_image.py"  # HERE = <oasis>/scenarios/tpch_q6


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--sf", type=float, default=0.01, help="TPC-H scale factor (default 0.01, 60 175 rows)")
    p.add_argument("--pack", type=Path, default=DEFAULT_PACK, help="pack_image.py of Oasis")
    args = p.parse_args()
    if not args.pack.is_file():
        print(f"prepare.py: packer {args.pack} not found, give --pack", file=sys.stderr)
        return 2
    import duckdb

    parquet = HERE / "lineitem.parquet"
    con = duckdb.connect()
    con.sql(f"INSTALL tpch; LOAD tpch; CALL dbgen(sf={args.sf})")
    con.sql(f"COPY lineitem TO '{parquet}' (FORMAT parquet)")
    rows = con.sql("SELECT count(*) FROM lineitem").fetchone()[0]
    print(f"{parquet.name}: {rows} rows, {parquet.stat().st_size} B")
    subprocess.run([sys.executable, str(args.pack), str(HERE / "image.bin"), str(parquet)], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
