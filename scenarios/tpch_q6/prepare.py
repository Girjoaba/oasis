"""Write lineitem.parquet (TPC-H dbgen) and pack it into image.bin for the RDMA mock.

Usage: prepare.py [--sf 0.01]

Needs the `duckdb` Python module (pip install duckdb). The first run downloads the
tpch extension.
The packer is rdma_server/pack_image.py of this checkout, so the image has the
layout of oasis_rdma_server.
"""

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK = HERE.parents[1] / "rdma_server" / "pack_image.py"  # HERE = <oasis>/scenarios/tpch_q6


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--sf", type=float, default=0.01, help="TPC-H scale factor (default 0.01, 60 175 rows)")
    args = p.parse_args()
    import duckdb

    parquet = HERE / "lineitem.parquet"
    con = duckdb.connect()
    con.sql(f"INSTALL tpch; LOAD tpch; CALL dbgen(sf={args.sf})")
    con.sql(f"COPY lineitem TO '{parquet}' (FORMAT parquet)")
    rows = con.sql("SELECT count(*) FROM lineitem").fetchone()[0]
    print(f"{parquet.name}: {rows} rows, {parquet.stat().st_size} B")
    subprocess.run([sys.executable, str(PACK), str(HERE / "image.bin"), str(parquet)], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
